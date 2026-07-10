import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// Deep link into the signed-in Gmail account. #all reaches a thread whatever
// its state (inbox, archived, labeled).
export function gmailThreadUrl(providerThreadId: string): string {
  return `https://mail.google.com/mail/#all/${encodeURIComponent(providerThreadId)}`;
}
