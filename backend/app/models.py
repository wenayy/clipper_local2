"""Persistent schema: users, jobs, clips.

Two things here exist for features that do not exist yet, and are worth calling
out so they are not mistaken for dead weight:

  Job.transcript   the full word-level Deepgram result. Re-running transcription
                   just to let someone drag a clip boundary would be absurd, so
                   the editor needs this stored the first time.
  Clip.words       the exact words in the rendered clip, so a trim/extend edit
                   can recompute captions without touching the source video.

User.credits is likewise the hook billing will decrement; it is enforced nowhere
yet, and anonymous jobs are still allowed.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer,
                        String, Text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime) -> str:
    """ISO 8601 with an explicit offset.

    SQLite has no timezone type, so a value written as aware UTC reads back
    NAIVE. `isoformat()` on that produces "2026-06-03T21:24:00" with no offset,
    and `new Date()` in the browser reads an offsetless string as LOCAL time --
    so a job created a minute ago showed up as "6 hours ago" for anyone east of
    UTC, off by exactly their own offset. Stamp the offset we actually mean.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # Credits buy finished clips; registration sets the current free grant
    # explicitly, while this default protects non-HTTP creation paths.
    credits: Mapped[int] = mapped_column(Integer, default=20)
    plan: Mapped[str] = mapped_column(String(32), default="free")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    jobs: Mapped[list["Job"]] = relationship(back_populates="user")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Nullable so the app keeps working without accounts; jobs made while signed
    # out simply have no owner.
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True, index=True
    )

    url: Mapped[str] = mapped_column(Text)
    options: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    progress_message: Mapped[str] = mapped_column(Text, default="")
    percent: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=True)

    # Kept so the editor can reopen a job without paying Deepgram again.
    transcript: Mapped[dict] = mapped_column(JSON, nullable=True)
    source_duration: Mapped[float] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    user: Mapped["User"] = relationship(back_populates="jobs")
    clips: Mapped[list["Clip"]] = relationship(
        back_populates="job", cascade="all, delete-orphan",
        order_by="Clip.index", lazy="selectin",
    )

    def _proxy_ready(self) -> bool:
        """Whether ANY scrubbing proxy for this job has finished building.

        Readiness is really per-clip now (each clip gets its own windowed
        proxy, and clip 1 is playable long before the last one), so the editor
        reads `proxy_ready` off the clip. This stays for the job-level views
        that only need to know whether live editing has started working at all.
        """
        import os
        storage = os.path.join(os.path.dirname(__file__), "..", "storage")
        try:
            return any(n.startswith("preview") and n.endswith(".mp4")
                       for n in os.listdir(os.path.join(storage, self.id)))
        except OSError:
            return False

    def to_dict(self) -> dict:
        """Shape the existing frontend already consumes."""
        return {
            "id": self.id,
            "url": self.url,
            "options": self.options or {},
            "status": self.status,
            "progress_message": self.progress_message or "",
            "percent": self.percent or 0,
            "error": self.error,
            "clips": [c.to_dict() for c in self.clips],
            # The editor plays this proxy; it is built AFTER the clips so results
            # appear sooner, so the editor must be able to tell "not ready yet"
            # from "broken" instead of showing a black frame.
            "proxy_ready": self._proxy_ready(),
            "created_at": _iso_utc(self.created_at),
        }


class CreditLedger(Base):
    """Every credit movement, so a balance can always be explained.

    A bare integer on the user row cannot answer "why am I down 40 credits?",
    which is the first question anyone asks about metered billing.
    """

    __tablename__ = "credit_ledger"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    delta: Mapped[int] = mapped_column(Integer)            # negative = spent
    balance_after: Mapped[int] = mapped_column(Integer)
    job_id: Mapped[str] = mapped_column(String(36), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PolarWebhookEvent(Base):
    """Processed delivery IDs stop Polar retries from repeating side effects."""

    __tablename__ = "polar_webhook_events"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PolarOrderGrant(Base):
    """One paid Polar order can grant credits exactly once."""

    __tablename__ = "polar_order_grants"

    order_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    product_id: Mapped[str] = mapped_column(String(255), index=True)
    credits: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PolarSubscription(Base):
    """Tracks which subscription currently owns a user's paid entitlement."""

    __tablename__ = "polar_subscriptions"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    product_id: Mapped[str] = mapped_column(String(255), index=True)
    plan_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


# --- Dodo Payments (current billing provider) ------------------------------
# Mirrors the Polar tables above. Kept separate rather than reused so a delivery
# ID from one provider can never collide with the other's, and so switching
# providers is additive instead of a migration of live payment records.


class DodoWebhookEvent(Base):
    """Processed delivery IDs stop Dodo retries from repeating side effects."""

    __tablename__ = "dodo_webhook_events"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DodoGrant(Base):
    """One paid Dodo charge can grant credits exactly once.

    Keyed by a per-charge id (a one-time payment_id, or a subscription's charge
    id) so a webhook redelivery -- or a manual confirm racing the webhook --
    cannot double-credit an account.
    """

    __tablename__ = "dodo_grants"

    charge_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    product_id: Mapped[str] = mapped_column(String(255), index=True, default="")
    credits: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DodoSubscription(Base):
    """Tracks which Dodo subscription currently owns a user's paid plan."""

    __tablename__ = "dodo_subscriptions"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    product_id: Mapped[str] = mapped_column(String(255), index=True, default="")
    # Nullable: a subscription whose Dodo product is not yet pasted into the
    # catalog still needs a row, and a NOT NULL here would abort the whole
    # fulfillment transaction (credits included) rather than just this record.
    plan_id: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    status: Mapped[str] = mapped_column(String(32), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class FreeEditorExport(Base):
    """The single edited export included with Free.

    A dedicated table lets existing databases adopt the rule through
    create_all without altering the users table. Signed-in accounts are unique
    by user; anonymous legacy jobs are unique by job.
    """

    __tablename__ = "free_editor_exports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True, unique=True, index=True
    )
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id"), unique=True, index=True
    )
    clip_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class QueuedJob(Base):
    """One piece of pending work, and which worker has it.

    A separate table rather than columns on Job, for the reason
    FreeEditorExport gives: create_all can adopt a new table on an existing
    database, and there is no migration tool here. It is also the honest shape.
    A Job is the thing a user owns and keeps; this row is scheduling state that
    exists only until the work is done, and then is deleted. Nothing here needs
    to outlive the run.

    Everything needed to RUN the job already lives on Job.options, frozen at
    submission -- so this row carries no copy of it. One source of truth for
    what was asked for, which is the same reason billing reads the frozen
    options rather than the live request.
    """

    __tablename__ = "job_queue"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Unique: a job is queued once. The claim is what makes it exclusive, and
    # two rows for one job would let two workers each claim "their" row and
    # render the same thing twice.
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id"), unique=True, index=True
    )

    # Higher runs first. Pro accounts get lifted above the queue -- the only
    # thing that makes billing's "priority" entitlement mean anything.
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)

    # Retries exist for the crash case, not the rejection case: a container
    # restart mid-render should be picked up again, while a private video will
    # fail identically every time and must not be retried into a loop. run()
    # decides which is which.
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # When this becomes eligible to run. Moves forward on retry so a failing
    # job backs off instead of spinning against the front of the queue.
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )

    # Null means unclaimed. Set together, always.
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    claimed_by: Mapped[str] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Share(Base):
    """A public page for one clip.

    Explicit and per clip rather than "every clip has a URL": a share link is a
    decision, and a guessable one for every clip ever rendered would make a
    private job public by default. The token is the capability -- there is no
    listing endpoint, so a link that was never shared cannot be found.
    """

    __tablename__ = "shares"

    token: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id"), index=True)
    clip_index: Mapped[int] = mapped_column(Integer, default=0)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"),
                                         nullable=True, index=True)
    views: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Clip(Base):
    __tablename__ = "clips"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id"), index=True
    )
    index: Mapped[int] = mapped_column(Integer, default=0)

    file: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text, default="")
    hook: Mapped[str] = mapped_column(Text, default="")

    start: Mapped[float] = mapped_column(Float, default=0.0)
    end: Mapped[float] = mapped_column(Float, default=0.0)
    duration: Mapped[float] = mapped_column(Float, default=0.0)

    score: Mapped[float] = mapped_column(Float, nullable=True)
    scores: Mapped[dict] = mapped_column(JSON, nullable=True)
    keywords: Mapped[list] = mapped_column(JSON, nullable=True)
    # Word-level timings for this clip, so trim/extend can rebuild captions.
    words: Mapped[list] = mapped_column(JSON, nullable=True)

    # The clip as a RECIPE rather than a baked file: everything needed to
    # reproduce it. The editor changes these instantly in the browser and only
    # pays for an ffmpeg render when the user exports.
    #   {ratio, frame, caption_style, caption_font, speed, translate_to,
    #    lines: [{start,end,text}], overlays: [{type,text,x,y,...}]}
    edit: Mapped[dict] = mapped_column(JSON, nullable=True)
    # Whether `file` reflects the current recipe. False after an unexported edit.
    rendered: Mapped[bool] = mapped_column(Boolean, default=True)

    job: Mapped["Job"] = relationship(back_populates="clips")

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "title": self.title,
            "hook": self.hook,
            "start": self.start,
            "end": self.end,
            "duration": self.duration,
            "score": self.score,
            "scores": self.scores or {},
            "keywords": self.keywords or [],
            "edit": self.edit or {},
            "rendered": bool(self.rendered),
            # Re-exporting overwrites clip_N.mp4 at the SAME url, so a browser
            # that already has it happily shows the pre-edit video -- which is
            # why an edited title appeared on the card but not in the picture.
            # The file's mtime changes on every render, so appending it as ?v=
            # makes each export a distinct url with no schema change.
            "version": self._file_version(),
            # This clip's scrubbing proxy covers a window around the clip rather
            # than the whole source, so the editor must know where that window
            # starts: proxy time zero is source time `proxy_offset`.
            **self._proxy_state(),
        }

    def _proxy_state(self) -> dict:
        """Whether this clip's scrubbing copy exists, and where it starts.

        Old jobs have a single whole-source preview.mp4 and no windowed file;
        they report offset 0, which is exactly right for a proxy that starts at
        the beginning of the source.
        """
        import os
        from app.pipeline import render
        storage = os.path.join(os.path.dirname(__file__), "..", "storage")
        job_dir = os.path.join(storage, self.job_id)
        # `index` is 0-based; the files rendering writes are 1-based, matching
        # clip_1.mp4 / preview_1.mp4.
        windowed = os.path.join(job_dir, f"preview_{self.index + 1}.mp4")
        if os.path.exists(windowed):
            lo, _ = render.proxy_window(self.start, self.end,
                                        self.job.source_duration if self.job else None)
            return {"proxy_ready": True, "proxy_offset": lo}
        return {"proxy_ready": os.path.exists(os.path.join(job_dir, "preview.mp4")),
                "proxy_offset": 0.0}

    def _file_version(self) -> int:
        # Resolved here rather than imported from app.main, which imports this
        # module -- the import would be circular.
        import os
        storage = os.path.join(os.path.dirname(__file__), "..", "storage")
        try:
            return int(os.path.getmtime(
                os.path.join(storage, self.job_id, self.file or "")))
        except OSError:
            return 0
