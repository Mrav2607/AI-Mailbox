/**
 * The product mark: an envelope carrying a synapse — the flap lines meet in a
 * filled node, mail with a cortex behind it. Inherits currentColor so it takes
 * the amber accent from its container.
 */
export function Mark({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="2.75" y="4.75" width="18.5" height="14.5" rx="2.5" />
      <path d="M3.5 7.5 12 13.25 20.5 7.5" />
      <circle cx="12" cy="13.25" r="1.6" fill="currentColor" stroke="none" />
    </svg>
  );
}
