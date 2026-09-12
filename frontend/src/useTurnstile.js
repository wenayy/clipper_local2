import { createElement, useEffect, useRef, useState, useCallback } from "react";
import { API_BASE } from "./api";

/**
 * Cloudflare Turnstile as a VISIBLE checkbox the caller places with <Turnstile/>.
 *
 * Why visible: job submission spends real money (download, transcription, LLM),
 * so a human check in front of the link box is the cheapest defence against
 * someone pasting a flood of URLs. Solving the checkbox stores a token;
 * getToken() hands back that token and immediately resets the widget so the next
 * submission gets a fresh, single-use one (Turnstile tokens cannot be reused).
 *
 * When Turnstile is not configured on the backend, <Turnstile/> renders nothing
 * and getToken() returns "" so submission still works in local dev.
 */
export default function useTurnstile() {
  const [siteKey, setSiteKey] = useState(null);
  // Once the check passes we collapse the (bulky) Cloudflare box and show a
  // small confirmation instead, so it stops dominating the form. The widget
  // stays mounted -- just hidden -- so getToken() can still reset it for the
  // next submission.
  const [verified, setVerified] = useState(false);
  const widgetId = useRef(null);
  const tokenRef = useRef("");
  const resolveRef = useRef(null);

  useEffect(() => {
    fetch(`${API_BASE}/turnstile-site-key`)
      .then((r) => r.json())
      .then((d) => setSiteKey(d.site_key || null))
      .catch(() => {});
  }, []);

  // Callback ref: React calls this with the DOM node on mount and null on
  // unmount, so the widget's lifecycle follows wherever <Turnstile/> is placed.
  const attach = useCallback((node) => {
    if (!node) {
      if (widgetId.current != null) {
        window.turnstile?.remove(widgetId.current);
        widgetId.current = null;
      }
      return;
    }
    if (!siteKey) return;

    const tryMount = () => {
      if (widgetId.current != null || !document.body.contains(node)) return;
      if (!window.turnstile) { setTimeout(tryMount, 200); return; }
      widgetId.current = window.turnstile.render(node, {
        sitekey: siteKey,
        callback: (token) => {
          tokenRef.current = token;
          setVerified(true);
          resolveRef.current?.(token);
          resolveRef.current = null;
        },
        "expired-callback": () => { tokenRef.current = ""; setVerified(false); },
        "error-callback": () => { tokenRef.current = ""; setVerified(false); },
      });
    };
    tryMount();
  }, [siteKey]);

  const getToken = useCallback(() => {
    if (!siteKey || widgetId.current == null) return Promise.resolve("");

    const current = tokenRef.current;
    if (current) {
      tokenRef.current = "";
      // Tokens are single-use; reset so the next submission earns a fresh one.
      try { window.turnstile.reset(widgetId.current); } catch { /* ignore */ }
      return Promise.resolve(current);
    }

    // Not solved yet — wait for the user to complete the visible challenge.
    return new Promise((resolve) => {
      resolveRef.current = resolve;
      setTimeout(() => {
        if (resolveRef.current) {
          resolveRef.current("");
          resolveRef.current = null;
        }
      }, 30000);
    });
  }, [siteKey]);

  const Turnstile = useCallback(
    (props) => {
      if (!siteKey) return null;
      return createElement(
        "div",
        props,
        // The actual widget: kept mounted, but collapsed once verified so the
        // big Cloudflare box disappears without unmounting (reset still works).
        createElement("div", {
          ref: attach,
          style: verified ? { height: 0, overflow: "hidden" } : undefined,
        }),
        // Subtle replacement shown after the check passes.
        verified
          ? createElement(
              "div",
              {
                style: {
                  display: "flex", alignItems: "center", gap: "6px",
                  fontSize: "13px", color: "#22c55e", opacity: 0.9,
                },
              },
              createElement("span", { "aria-hidden": "true" }, "✓"),
              "Verified — you're human",
            )
          : null,
      );
    },
    [siteKey, attach, verified],
  );

  return { getToken, Turnstile };
}
