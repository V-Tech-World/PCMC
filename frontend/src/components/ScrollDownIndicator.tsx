import { useEffect, useState } from "react";

/**
 * Small scroll-down hint (extra feature, deliberately understated): a 36px
 * chevron pill in the bottom-right corner that only exists while there is
 * MORE page below, and disappears once you are near the bottom. Clicking it
 * scrolls about one screen down.
 */
export default function ScrollDownIndicator() {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const check = () => {
      const doc = document.documentElement;
      const scrollable = doc.scrollHeight > window.innerHeight + 140;
      const nearBottom =
        window.innerHeight + window.scrollY >= doc.scrollHeight - 140;
      setVisible(scrollable && !nearBottom);
    };
    check();
    window.addEventListener("scroll", check, { passive: true });
    window.addEventListener("resize", check);
    return () => {
      window.removeEventListener("scroll", check);
      window.removeEventListener("resize", check);
    };
  }, []);

  if (!visible) return null;

  return (
    <button
      type="button"
      aria-label="Scroll down"
      title="Scroll down"
      onClick={() =>
        window.scrollBy({ top: window.innerHeight * 0.8, behavior: "smooth" })
      }
      className="fixed bottom-5 right-5 z-40 grid h-9 w-9 place-items-center rounded-full bg-white/90 text-neutral-500 opacity-70 shadow-md ring-1 ring-neutral-200 transition-all hover:text-brand-700 hover:opacity-100 hover:shadow-lg active:scale-90 dark:bg-neutral-800/90 dark:text-neutral-300 dark:ring-neutral-600 dark:hover:text-brand-300"
    >
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="nudge h-4 w-4"
        aria-hidden="true"
      >
        <path d="M6 9l6 6 6-6" />
      </svg>
    </button>
  );
}