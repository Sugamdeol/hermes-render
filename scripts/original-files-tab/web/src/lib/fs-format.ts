// Shared human-readable byte formatting for the Files tab (list, analysis card, search).
export function formatBytes(size: number | null | undefined): string {
  if (size === null || size === undefined || !Number.isFinite(size)) return "-";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  return `${(size / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}
