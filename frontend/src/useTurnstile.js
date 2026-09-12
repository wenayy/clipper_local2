import { useEffect, useRef, useState, useCallback } from "react";
import { API_BASE } from "./api";

/**
 * Manages a Cloudflare Turnstile widget that lives in an invisible container.
 * Returns getToken() — call it before each job submission to get a fresh token.
 * When Turnstile is not configured on the backend, getToken() returns "".
 */
export default function useTurnstile() {
  const [siteKey, setSiteKey] = useState(null);
  const widgetId = useRef(null);
  const containerRef = useRef(null);
  const resolveRef = useRef(null);

  useEffect(() => {
    fetch(`${API_BASE}/turnstile-site-key`)
      .then((r) => r.json())
      .then((d) => setSiteKey(d.site_key || null))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!siteKey) return;

    const container = document.createElement("div");
    container.style.position = "fixed";
    container.style.bottom = "0";
    container.style.right = "0";
    container.style.zIndex = "9999";
    document.body.appendChild(container);
    containerRef.current = container;

    function mount() {
      if (!window.turnstile || widgetId.current != null) return;
      widgetId.current = window.turnstile.render(container, {
        sitekey: siteKey,
        size: "invisible",
        callback: (token) => {
          resolveRef.current?.(token);
          resolveRef.current = null;
        },
        "error-callback": () => {
          resolveRef.current?.("");
          resolveRef.current = null;
        },
      });
    }

    if (window.turnstile) {
      mount();
    } else {
      const interval = setInterval(() => {
        if (window.turnstile) {
          clearInterval(interval);
          mount();
        }
      }, 200);
      return () => {
        clearInterval(interval);
        if (widgetId.current != null) window.turnstile?.remove(widgetId.current);
        container.remove();
      };
    }

    return () => {
      if (widgetId.current != null) window.turnstile?.remove(widgetId.current);
      container.remove();
    };
  }, [siteKey]);

  const getToken = useCallback(() => {
    if (!siteKey || widgetId.current == null) return Promise.resolve("");
    window.turnstile.reset(widgetId.current);
    return new Promise((resolve) => {
      resolveRef.current = resolve;
      window.turnstile.execute(widgetId.current);
      setTimeout(() => {
        if (resolveRef.current) {
          resolveRef.current("");
          resolveRef.current = null;
        }
      }, 15000);
    });
  }, [siteKey]);

  return { getToken };
}
