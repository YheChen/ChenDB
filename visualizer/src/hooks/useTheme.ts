/**
 * Light/dark theme, persisted and applied to `<html>`.
 *
 * The initial class is set by an inline script in `index.html` before first
 * paint; this hook only handles changes afterwards, so there is no flash of the
 * wrong theme on load.
 */

import { useCallback, useEffect, useState } from "react";

const STORAGE_KEY = "chendb.theme";

export type Theme = "light" | "dark";

function currentTheme(): Theme {
  if (typeof document === "undefined") return "dark";
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(currentTheme);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // Private browsing can block storage; the theme still applies this session.
    }
  }, [theme]);

  const toggle = useCallback(() => {
    setTheme((current) => (current === "dark" ? "light" : "dark"));
  }, []);

  return [theme, toggle];
}
