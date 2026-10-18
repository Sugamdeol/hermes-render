import { useEffect, useRef, useState } from "react";
import { authedFetch } from "@/lib/api";

type Entry = { name: string; path?: string; size: number; is_directory?: boolean };
type Report = { bytes: number; files: number; directories: number; partial: boolean; skipped: number; children: Entry[]; largest: Entry[]; matches: Entry[]; result_limit: number };
const bytes = (value: number) => value < 1024 ? `${value} B` : value < 1048576 ? `${(value / 1024).toFixed(1)} KB` : value < 1073741824 ? `${(value / 1048576).toFixed(1)} MB` : `${(value / 1073741824).toFixed(2)} GB`;

export default function FilesStorageTools({ path, onNavigate }: { path: string; onNavigate: (path: string) => void }) {
  const [report, setReport] = useState<Report | null>(null);
  const [results, setResults] = useState<Report | null>(null);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = useRef<AbortController | null>(null);
  useEffect(() => { active.current?.abort(); setReport(null); setResults(null); setBusy(false); setError(""); return () => active.current?.abort(); }, [path]);
  async function run(search: boolean) {
    active.current?.abort();
    const controller = new AbortController(); active.current = controller;
    setBusy(true); setError("");
    try {
      const params = new URLSearchParams({ path });
      if (search) params.set("q", query.trim());
      const response = await authedFetch(`/api/fs/${search ? "search" : "analysis"}?${params}`, { signal: controller.signal });
      if (!response.ok) throw new Error(`Request failed (${response.status}). Refresh and retry.`);
      const data: Report = await response.json();
      if (!controller.signal.aborted) { if (search) setResults(data); else setReport(data); }
    } catch (e) { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "Could not read storage"); }
    finally { if (active.current === controller) setBusy(false); }
  }
  return <section className="rounded-xl border p-4 space-y-3" aria-label="Storage analysis and recursive search">
    <div className="flex flex-wrap items-center gap-3">
      <strong>Storage analysis</strong>
      <button className="rounded border px-3 py-1" disabled={busy || !path} onClick={() => void run(false)}>{busy ? "Scanning…" : report ? "Refresh analysis" : "Analyse folder"}</button>
    </div>
    <form className="flex flex-wrap gap-2" onSubmit={e => { e.preventDefault(); void run(true); }}>
      <input className="min-w-0 flex-1 rounded border bg-transparent px-3 py-2" aria-label="Search files recursively" placeholder="Search this folder and subfolders…" value={query} onChange={e => setQuery(e.target.value)} maxLength={200} />
      <button className="rounded border px-3 py-1" disabled={busy || !path || !query.trim()}>Search</button>
    </form>
    {error && <p role="alert">{error}</p>}
    {report && <div className="space-y-2 text-sm">
      <p>{bytes(report.bytes)} · {report.files.toLocaleString()} files · {report.directories.toLocaleString()} folders{report.partial ? " · Scan limit reached: partial totals" : ""}</p>
      <p>Credential paths, symlinks and other mounted filesystems are excluded ({report.skipped} skipped).</p>
      <details><summary>Folder sizes</summary><ul>{report.children.slice(0, 100).map(item => <li key={item.name} className="flex justify-between gap-3 py-1">{item.is_directory ? <button className="underline text-left break-all" onClick={() => item.path && onNavigate(item.path)}>{item.name}/</button> : <span className="break-all">{item.name}</span>}<span>{bytes(item.size)}</span></li>)}</ul></details>
      <details><summary>Largest files</summary><ul>{report.largest.map(item => <li key={item.name} className="flex justify-between gap-3 py-1"><span className="break-all">{item.name}</span><span>{bytes(item.size)}</span></li>)}</ul></details>
    </div>}
    {results && <div className="text-sm"><p>{results.matches.length} matches (up to {results.result_limit}){results.partial ? " · Partial scan" : ""}</p><ul>{results.matches.map(item => <li key={item.path} className="py-1 break-all">{item.is_directory ? <button className="underline" onClick={() => item.path && onNavigate(item.path)}>{item.name}/</button> : item.name}</li>)}</ul></div>}
  </section>;
}
