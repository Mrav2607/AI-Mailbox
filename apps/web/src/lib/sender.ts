function stripSurroundingQuotes(value: string): string {
  const first = value[0];
  const last = value[value.length - 1];
  if (value.length >= 2 && first === last && (first === '"' || first === "'")) {
    return value.slice(1, -1).trim();
  }
  return value;
}

// Exported for the multi-account badges (ThreadList / ThreadDetailPane),
// which show just the local part of an account's address, not a full sender.
export function emailLocalPart(value: string): string {
  return value.split("@", 1)[0]?.trim() ?? "";
}

export function senderName(raw: string | null): string | null {
  const value = raw?.trim();
  if (!value) return null;

  const angleAddress = value.match(/^(.*?)\s*<([^<>]+)>$/);
  if (angleAddress) {
    const displayName = stripSurroundingQuotes(angleAddress[1].trim());
    return displayName || emailLocalPart(angleAddress[2]) || null;
  }

  return emailLocalPart(value) || null;
}
