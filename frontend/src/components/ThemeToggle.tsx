import { useState } from "react";

const THEME_KEY = "voicecare-theme";

/**
 * Sun/moon theme toggle (top bar). Default is LIGHT: the `.dark` class is
 * only present when the user previously chose dark (bootstrap script in
 * index.html applies it before first paint, so there is no flash).
 */
export default function ThemeToggle() {
  const [dark, setDark] = useState<boolean>(() =>
    document.documentElement.classList.contains("dark"),
  );

  function toggle() {
    const next = !dark;
    setDark(next);
    document.documentElement.classList.toggle("dark", next);
    try {
      localStorage.setItem(THEME_KEY, next ? "dark" : "light");
    } catch {
      /* private mode: theme still applies for this session */
    }
  }

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={dark ? "Switch to light mode" : "Switch to dark mode"}
      title={dark ? "Light mode" : "Dark mode"}
      className="grid h-9 w-9 cursor-pointer place-items-center rounded-full text-neutral-600 ring-1 ring-neutral-300 transition-all hover:bg-white/70 hover:text-brand-700 active:scale-90 dark:text-neutral-300 dark:ring-neutral-600 dark:hover:bg-white/10 dark:hover:text-brand-300"
    >
      <span key={dark ? "moon" : "sun"} className="pop-in">
        {dark ? (
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-[18px] w-[18px]"
            aria-hidden="true"
          >
            <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
          </svg>
        ) : (
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-[18px] w-[18px]"
            aria-hidden="true"
          >
            <circle cx="12" cy="12" r="4" />
            <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
          </svg>
        )}
      </span>
    </button>
  );
}