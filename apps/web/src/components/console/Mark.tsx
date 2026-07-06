/**
 * The product mark: a terminal-prompt glyph (a chevron + cursor rule) instead
 * of the stock "AI sparkles" everyone ships. Inherits currentColor so it takes
 * the amber accent from its container.
 */
export function Mark({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.25}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M6.5 7.5 11 12l-4.5 4.5" />
      <path d="M13 16.5h5" />
    </svg>
  );
}
