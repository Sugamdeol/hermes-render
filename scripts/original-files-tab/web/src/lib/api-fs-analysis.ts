// Files-tab storage analysis API: /api/fs/analysis, /api/fs/search, /api/fs/zip.
// Types live here (not api-files.ts) because these endpoints serve the
// analysis/search/zip surface, not the managed-files browser CRUD.

import { apiErrorFromResponse } from "./api-error";
import { authedFetch, fetchJSON } from "./api";

export interface FsDiskUsage {
  total: number;
  used: number;
  free: number;
  percent: number;
  device: string | null;
}

export interface FsAnalysisTotals {
  files: number;
  dirs: number;
  symlinks: number;
  bytes: number;
  other_devices: number;
}

export interface FsAnalysisChild {
  name: string;
  path: string;
  is_directory: boolean;
  bytes: number;
  files: number;
  dirs: number;
}

export interface FsTypeStat {
  ext: string;
  count: number;
  bytes: number;
}

export interface FsAnalysisEntry {
  name: string;
  path: string;
  is_directory: boolean;
  size: number | null;
  mtime: number | null;
}

export interface FsAnalysisResponse {
  path: string;
  root: string | null;
  locked_root: string | null;
  can_change_path: boolean;
  generated_at: number;
  elapsed_ms: number;
  cached: boolean;
  truncated: boolean;
  disk: FsDiskUsage | null;
  totals: FsAnalysisTotals;
  children: FsAnalysisChild[];
  types: FsTypeStat[];
  largest: FsAnalysisEntry[];
  recent: FsAnalysisEntry[];
  mount_points: string[];
}

export interface FsSearchMatch {
  name: string;
  path: string;
  is_directory: boolean;
  size: number | null;
  mtime: number | null;
  broken_link?: boolean;
}

export interface FsSearchResponse {
  path: string;
  root: string | null;
  query: string;
  matches: FsSearchMatch[];
  truncated: boolean;
  elapsed_ms: number;
}

export function analyzeStorage(path: string, opts?: { fresh?: boolean; topN?: number }) {
  const qs = new URLSearchParams({ path });
  if (opts?.fresh) qs.set("fresh", "true");
  if (opts?.topN) qs.set("top_n", String(opts.topN));
  return fetchJSON<FsAnalysisResponse>(`/api/fs/analysis?${qs.toString()}`);
}

export function searchFiles(path: string, query: string, limit = 60) {
  const qs = new URLSearchParams({ path, query, limit: String(limit) });
  return fetchJSON<FsSearchResponse>(`/api/fs/search?${qs.toString()}`);
}

/** Stream a directory subtree as a zip attachment (server caps apply). */
export async function downloadDirectoryZip(path: string, name: string): Promise<void> {
  const res = await authedFetch(`/api/fs/zip?path=${encodeURIComponent(path)}`);
  if (!res.ok) {
    throw apiErrorFromResponse(res.status, await res.text().catch(() => ""), res.url);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${name || "files"}.zip`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
