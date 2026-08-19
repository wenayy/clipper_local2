# Video Encoding: From Basics to How KlipCut Uses It

A complete walkthrough of how video encoding works, what every flag in the render
pipeline does, and why the decisions were made this way. Written so you can explain
it in an interview with confidence.

---

## 1. What Is a Video File, Really?

A video file (.mp4) is NOT a sequence of images stored one after another. That
would be enormous -- a 1080x1920 frame at 24-bit color is ~6 MB. At 30 fps,
one minute would be 10.8 GB.

Instead, a video file is:

```
.mp4 file
  |
  +-- Container (MP4)         <-- the box
  |     |
  |     +-- Video stream      <-- compressed frames (H.264 codec)
  |     +-- Audio stream      <-- compressed sound (AAC codec)
  |     +-- Metadata          <-- duration, resolution, framerate, etc.
  |
  +-- moov atom               <-- index: where each frame lives in the file
```

**Container** = the file format (MP4, MKV, WebM). It's just packaging -- a zip
file for media streams. It doesn't touch quality.

**Codec** = the algorithm that compresses/decompresses the actual pixels and
audio samples. This is where all the interesting engineering happens.

**Stream** = one track of data inside the container. A typical video has one
video stream and one audio stream, but can have more (subtitles, multiple audio
languages, etc).

---

## 2. How H.264 Compression Works

H.264 (also called AVC) is the most widely used video codec in the world. Every
phone, browser, social platform, and streaming service supports it. It's what
`libx264` encodes.

### The Core Idea: Don't Store What Didn't Change

H.264 uses three types of frames:

```
I-frame (Intra)     -- a full image, compressed like a JPEG
                       Self-contained. Can be decoded on its own.
                       These are the "keyframes."

P-frame (Predicted) -- only stores DIFFERENCES from the previous frame
                       "The background didn't move. This person moved 3px right.
                       This region changed color."
                       Much smaller than an I-frame.

B-frame (Bi-dir)    -- stores differences from BOTH the previous AND next frame
                       Even smaller, because it can pick the better reference.
                       "This pixel is closer to the next frame than the previous."
```

A typical H.264 stream looks like:

```
I  B  B  P  B  B  P  B  B  P  B  B  I  B  B  P ...
|<----------- GOP (Group of Pictures) -------->|
```

An I-frame might be 50 KB. A P-frame might be 5 KB. A B-frame might be 2 KB.
That's how you get 100:1 compression ratios.

### Motion Estimation (Where the CPU Time Goes)

For each P-frame and B-frame, the encoder must:

1. Divide the frame into blocks (16x16, 8x8, 4x4 pixels)
2. For each block, search the reference frame(s) to find where that content moved
3. Encode the motion vector ("this block moved from (x,y) to (x+3, y-1)")
4. Encode the residual (what's left after subtracting the prediction)
5. Apply a frequency transform (DCT -- like JPEG) to the residual
6. Quantize the transform coefficients (lossy step -- this is where quality is lost)
7. Entropy-code the result (lossless compression of the final numbers)

Steps 2-3 are BY FAR the most expensive. The encoder is literally searching for
matches across the entire reference frame for every single block. A "slow" preset
searches harder (more reference frames, larger search windows, more block sizes
to try). A "veryfast" preset searches less.

---

## 3. What CRF Means (And Why It's Not a "Quality Number")

CRF = Constant Rate Factor. It's x264's default rate control mode.

```
crf 0  = mathematically lossless (huge files)
crf 18 = visually lossless (most people can't see artifacts)
crf 23 = x264 default (good quality, reasonable size)
crf 28 = acceptable for streaming
crf 51 = maximum compression (looks terrible)
```

**What CRF actually does:** it targets a constant PERCEPTUAL quality across the
entire clip. Easy frames (static background) get fewer bits. Complex frames
(fast motion, confetti, lots of detail) get more bits. The file size varies --
CRF doesn't control size, it controls quality.

**CRF is logarithmic.** Each +6 roughly doubles the file size. So:
- crf 20 -> crf 14: file is ~4x larger, barely looks different
- crf 20 -> crf 26: file is ~4x smaller, noticeably worse

### In KlipCut's Code (render.py:845-849):

```python
# High quality (paid option):
"-crf", "17"   # near-visually-lossless, for archival/re-editing

# Normal quality:
"-crf", "20"   # excellent quality, much smaller files
```

The difference between 17 and 20 is about 2x file size. The visual difference is
nearly invisible on a phone screen -- which is where 95% of short-form clips are
watched. That's why high quality is a paid option: you're paying for encoder time
and storage, not for a difference the viewer will notice.

---

## 4. Presets: Speed vs Compression Efficiency

The preset controls HOW HARD the encoder tries to compress. It does NOT affect
quality -- it affects how efficiently it achieves that quality.

```
ultrafast  -->  veryfast  -->  fast  -->  medium  -->  slow  -->  veryslow
  |                                                                  |
  Speed                                                         Efficiency
  Big files                                                    Small files
  Same quality                                                 Same quality
```

At the same CRF, `slow` and `veryfast` produce the SAME visual quality. But:
- `slow` might produce a 10 MB file
- `veryfast` might produce a 14 MB file

The `slow` preset achieves the same quality with fewer bits by trying more
prediction modes, more reference frames, and more subpixel refinements.

### In KlipCut's Code:

```python
# High quality:
"-preset", "slow"       # smaller file, 5-8x slower

# Normal:
"-preset", "veryfast"   # larger file, but much faster
```

Why `veryfast` as default? Because:
1. The output goes to TikTok/Shorts which re-compresses it anyway
2. A user watching a progress bar cares about speed
3. The 30-40% file size difference is irrelevant after re-compression

---

## 5. Software vs Hardware Encoding

### Software Encoder: libx264

This is what KlipCut uses. It's a C library that runs on the CPU.

**How it works:**
- The CPU runs the full H.264 algorithm: motion estimation, DCT transforms,
  entropy coding, everything.
- It uses SIMD instructions (SSE, AVX on x86; NEON on ARM) to process multiple
  pixels per clock cycle, but it's still general-purpose silicon doing math.

**Pros:**
- Best compression efficiency (smallest files at given quality)
- Maximum control over every encoding parameter
- Runs on any machine with a CPU

**Cons:**
- Slow. A 60-second 1080p clip at `veryfast` takes ~15-30 seconds.
- CPU-hungry. Saturates all available cores.
- Memory-hungry. Allocates per-thread frame buffers.

### Hardware Encoder: h264_videotoolbox (macOS)

Apple's VideoToolbox is a framework that routes encoding to dedicated silicon
on the chip (the "media engine" on Apple Silicon, or Intel Quick Sync on older
Macs).

**How it works:**
- The frames go to a fixed-function ASIC (Application-Specific Integrated Circuit)
  that does H.264 encoding in hardware.
- This chip can ONLY do video encode/decode. It can't run Python or play games.
  It's like how a GPU can only do graphics -- except even more specialized.
- Because the algorithm is baked into the silicon, it runs at a fixed speed
  regardless of "difficulty."

**Pros:**
- 5-10x faster than libx264
- Almost no CPU usage (the CPU is free to do other work)
- Low power consumption

**Cons:**
- Slightly worse compression efficiency (~10-20% larger files at same visual quality)
- Fewer tuning knobs (no CRF equivalent -- uses a different quality scale)
- Platform-specific (VideoToolbox = macOS only; NVENC = NVIDIA only; VAAPI = Linux)

### Other Hardware Encoders (for reference):

| Encoder             | Platform         | ffmpeg flag           |
|---------------------|------------------|-----------------------|
| VideoToolbox        | macOS (any Mac)  | `h264_videotoolbox`   |
| NVENC               | NVIDIA GPUs      | `h264_nvenc`          |
| Quick Sync (QSV)    | Intel CPUs       | `h264_qsv`           |
| VAAPI               | Linux (AMD/Intel)| `h264_vaapi`          |
| MediaCodec          | Android          | `h264_mediacodec`     |

All produce valid H.264 output. The differences are in compression efficiency
and available tuning parameters.

---

## 6. Threads and Memory (The OOM Problem)

### How x264 Threading Works

x264 splits the frame into horizontal "slices" and encodes them in parallel:

```
Frame (1080 x 1920):
+---------------------------+
|     Slice 1 (Thread 1)    |   Each thread needs its own copy of:
+---------------------------+   - The current frame's slice
|     Slice 2 (Thread 2)    |   - Reference frame(s) for motion search
+---------------------------+   - Working buffers for DCT/quantization
|     Slice 3 (Thread 3)    |
+---------------------------+   At 1080x1920, each thread's buffers are ~50-100 MB
|     Slice 4 (Thread 4)    |
+---------------------------+
```

**The container OOM problem:**

x264 calls `sysconf(_SC_NPROCESSORS_ONLN)` to decide thread count. This returns
the HOST machine's CPU count -- not the container's.

```
Host machine:     48 cores, 128 GB RAM
Your container:    2 cores,   1 GB RAM

x264 sees:        48 cores -> allocates 48 threads
Each thread:      ~50 MB of frame buffers
Total:            48 x 50 MB = ~2.4 GB
Container limit:  1 GB
Result:           SIGKILL (OOM killer)
```

### In KlipCut's Code (render.py:14-52):

```python
def _container_cpus() -> int:
    """Read the REAL CPU quota from cgroups, not the host's count."""
    # Checks /sys/fs/cgroup/cpu.max (cgroups v2)
    # and /sys/fs/cgroup/cpu/cpu.cfs_quota_us (cgroups v1)
    # Falls back to os.cpu_count() if neither exists (bare metal / macOS)

FFMPEG_THREADS = max(1, int(os.environ.get("CLIPPER_FFMPEG_THREADS", "0"))
                     or _container_cpus())
```

This is the fix for the OOM crash you saw on Railway. Instead of letting x264
see the host's 48 cores and allocate 48 threads worth of memory, the code reads
the container's actual CPU quota from cgroups (Linux kernel's resource control)
and caps threads to that.

On your Mac, `_container_cpus()` falls through to `os.cpu_count()` (no cgroups
on macOS), which returns your real core count -- which is correct because on a
Mac the memory matches the CPU count.

---

## 7. The Filter Graph (What Happens Before Encoding)

Before a single frame reaches the encoder, ffmpeg processes it through a "filter
graph." In KlipCut, this is the most complex part.

### What a Filter Graph Looks Like

```
-filter_complex "[0:v]scale=540:960...,gblur=sigma=12,scale=1080:1920[bg];
                 [0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];
                 [bg][fg]overlay=(W-w)/2:(H-h)/2[framed];
                 [framed]subtitles='clip_1.ass'[v]"
```

This is a pipeline of pixel transformations, read left to right:

```
Source video
    |
    +---> scale down to 540x960 --> blur (sigma=12) --> scale up to 1080x1920 --> [bg]
    |                                                                              |
    +---> scale to fit 1080x1920 keeping aspect ratio ----------------------> [fg] |
                                                                               |   |
                                                                    overlay fg on bg
                                                                               |
                                                                           [framed]
                                                                               |
                                                                      burn in subtitles
                                                                               |
                                                                             [v] --> encoder
```

### Why Blur at Half Resolution? (render.py:54-59)

```python
REFRAME_FILTER = (
    "[0:v]scale=540:960:force_original_aspect_ratio=increase,"
    "crop=540:960,gblur=sigma=12,scale=1080:1920[bg];"
```

Gaussian blur is O(n) per pixel per sigma unit. At 1080x1920 (2 million pixels)
with sigma=12, that's expensive. But the background is going to be heavily blurred
anyway -- it's decorative, not informational. So:

1. Scale DOWN to 540x960 (quarter the pixels)
2. Blur at the small size (4x less work)
3. Scale UP to 1080x1920

The upscale after a heavy blur is invisible because all the detail was already
destroyed by the blur. This is a ~4x speedup for the blur step.

### The Different Frame Modes

KlipCut supports several ways to present a horizontal video in a vertical frame:

```
blur (default):    Blurred background + centered video
                   [================]
                   [################]  <-- blurred, zoomed source
                   [  +----------+  ]
                   [  |  actual  |  ]  <-- original, scaled to fit
                   [  |  video   |  ]
                   [  +----------+  ]
                   [################]
                   [================]

podcast:           Face-tracked dynamic crop
                   Detects faces, crops to follow the speaker
                   Uses reframe.plan() for keyframed panning

fill:              Zoom to fill, crop the sides
                   Loses content but no black bars

fit:               Scale to fit with optional zoom behavior

split:             Gameplay on top, camera on bottom (or vice versa)
```

---

## 8. Audio Encoding

```python
"-c:a", "aac", "-b:a", "192k"
```

**AAC** (Advanced Audio Coding) is the standard audio codec for MP4 containers.

**192 kbps** is the bitrate -- how many bits per second of audio. For reference:
- 128k = FM radio quality
- 192k = CD-like quality (what Spotify uses for "high")
- 320k = maximum MP3 quality
- 192k AAC sounds better than 192k MP3 (AAC is a more efficient codec)

Audio encoding is fast and cheap compared to video. A 60-second clip's audio
encodes in under a second.

---

## 9. Container Flags

### `-movflags +faststart` (render.py:863)

An MP4 file normally puts the "moov atom" (the index of where every frame is) at
the END of the file. This means a browser must download the entire file before it
can start playing.

`+faststart` moves the moov atom to the BEGINNING. Now the browser can start
playing after downloading just the first few kilobytes, because it immediately
knows where every frame is and can request them by byte range.

This is critical for web playback and for the R2/S3 pre-signed URLs KlipCut uses.

### `-pix_fmt yuv420p` (render.py:860)

Pixel format. YUV420p means:
- Y (brightness) at full resolution
- U and V (color) at half resolution in both dimensions

This is the universal compatibility format. Every device, player, and platform
supports it. Other formats (yuv444p, yuv420p10le for HDR) are higher quality but
not universally playable.

Human eyes are more sensitive to brightness than color, so halving color resolution
is nearly invisible. This saves 50% of the raw pixel data compared to full-color
(yuv444p).

---

## 10. Putting It All Together: What Happens When KlipCut Renders a Clip

The full render command for one clip:

```
ffmpeg
  -y                                    # overwrite output
  -ss 45.2                              # seek to clip start (fast, before -i)
  -i source.mp4                         # input video

  -filter_complex "                     # pixel pipeline:
    [0:v]scale=540:960:...,             #   1. shrink source for blur
    crop=540:960,                        #   2. crop to exact size
    gblur=sigma=12,                      #   3. blur (cheap at small size)
    scale=1080:1920[bg];                 #   4. scale up for background

    [0:v]scale=1080:1920:               #   5. scale original to fit
    force_original_aspect_ratio=decrease[fg];

    [bg][fg]overlay=(W-w)/2:(H-h)/2     #   6. center original on blurred bg
    [framed];

    [framed]subtitles='clip_1.ass'[v]   #   7. burn in captions
  "

  -map [v]                              # use the filtered video
  -map 0:a                              # keep original audio
  -t 38.5                               # clip duration

  -c:v libx264                          # H.264 software encoder
  -preset veryfast                      # fast encoding, slightly larger file
  -crf 20                               # excellent visual quality
  -threads 8                            # bounded to container's CPU count

  -pix_fmt yuv420p                      # universal compatibility
  -c:a aac -b:a 192k                    # good audio quality
  -movflags +faststart                  # web-playable without full download

  clip_1.mp4                            # output
```

### Time Breakdown for One 45-Second Clip (approximate):

| Step                  | Time     | What limits it          |
|-----------------------|----------|-------------------------|
| Seek + decode source  | 0.5s     | Disk I/O                |
| Blur filter (at 540p) | 3-5s     | CPU (per-pixel math)    |
| Scale + overlay       | 1-2s     | CPU + memory bandwidth  |
| Subtitle burn-in      | 1-3s     | CPU (text rendering)    |
| H.264 encode          | 10-20s   | CPU (motion estimation) |
| Audio encode          | <0.5s    | Trivial                 |
| Write to disk         | <0.5s    | Disk I/O                |
| **Total per clip**    | **~20s** |                         |
| **6 clips sequential**| **~2 min**|                        |

If your 6 clips are taking 30 minutes, the source video is likely very long/high-res
(the decoder has to read more data), or you're using high_quality mode (preset=slow,
which is 5-8x slower than veryfast).

---

## 11. Why Social Platforms Re-Compress (And Why It Matters)

When you upload a clip to TikTok/Shorts/Reels, they ALWAYS re-encode it:

1. **Normalize resolution** -- everything becomes their target bitrate
2. **Add DRM/watermarks** -- platform-specific metadata
3. **Optimize for streaming** -- their CDN needs specific segment sizes
4. **Reduce storage costs** -- your 10 MB clip becomes 3 MB on their servers

This means:
- Your crf 18 vs crf 22 difference? Gone after re-compression.
- Your `slow` preset's 30% smaller file? Irrelevant.
- VideoToolbox's slightly less efficient compression? Nobody will know.

The ONLY things that survive re-compression:
- Resolution (upload at 1080x1920 or higher)
- Frame rate (30 fps is fine; 60 fps is kept if uploaded)
- Audio quality (they preserve it better than video)
- The actual content of the pixels (obviously)

This is why KlipCut defaults to `veryfast`/`crf 20` -- it's the sweet spot where
the encode is fast enough for a good user experience and the quality is high enough
that the platform's re-compression has plenty to work with.

---

## 12. Interview-Ready Summary

**"How does your video rendering pipeline work?"**

> We take a source video, run it through an ffmpeg filter graph that creates a
> vertical frame (blurred background with the original centered), burns in styled
> captions from a .ass subtitle file, and encodes it with H.264 using libx264.
> We use CRF-based rate control for consistent perceptual quality, and default to
> the `veryfast` preset because the output goes to social platforms that re-compress
> everything anyway.

**"How did you handle the OOM crashes in containers?"**

> x264 sizes its thread pool from the CPU count it can see, which inside a
> container is the host's -- not the container's. So on a 48-core host with a 1 GB
> container, x264 would allocate 48 threads' worth of frame buffers (~2.4 GB) and
> get OOM-killed. We read the real CPU quota from cgroups and pass `-threads N`
> to cap it. We also added an environment variable override for cases where the
> operator knows better than the auto-detection.

**"Why not use hardware encoding in production?"**

> Hardware encoders like NVENC or VideoToolbox are 5-10x faster but platform-
> specific. Our workers run on Linux VMs without GPUs, so libx264 (CPU) is the
> only option. Locally on macOS we could use VideoToolbox for faster iteration,
> falling back to libx264 when it's not available. The visual quality difference
> is negligible for social media clips.

**"What's the difference between a codec and a container?"**

> The container (MP4) is the packaging -- it says "here's a video stream at byte
> offset X and an audio stream at byte offset Y." The codec (H.264, AAC) is the
> compression algorithm that turns raw pixels/samples into a compact bitstream.
> You can put H.264 video in an MP4, MKV, or AVI container -- same codec,
> different packaging.

**"Why blur at half resolution?"**

> Gaussian blur is per-pixel work proportional to the image size. Since the
> blurred background is purely decorative -- all detail is intentionally destroyed --
> we blur at quarter resolution (540x960 instead of 1080x1920) and scale back up.
> The upscale is invisible after a heavy blur. It's a 4x speedup for free.
