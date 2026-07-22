import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// Deep link into a Gmail account. #all reaches a thread whatever its state
// (inbox, archived, labeled). With `accountEmail` the link targets that
// specific signed-in Google account (authuser) instead of Gmail's default —
// necessary once a mailbox can span more than one connected account.
export function gmailThreadUrl(providerThreadId: string, accountEmail?: string): string {
  const path = `#all/${encodeURIComponent(providerThreadId)}`;
  if (accountEmail) {
    return `https://mail.google.com/mail/?authuser=${encodeURIComponent(accountEmail)}${path}`;
  }
  return `https://mail.google.com/mail/${path}`;
}
