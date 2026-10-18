import { BarChart3, Clock, Database, FileIcon, FolderClosed, HardDrive, RefreshCw, X } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import type { FsAnalysisChild, FsAnalysisEntry, FsAnalysisResponse, FsTypeStat } from "@/lib/api-fs-analysis";
import { formatBytes } from "@/lib/fs-format";

const DATE_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

const SHORT_DATE = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

interface FilesAnalysisCardProps {
  data: FsAnalysisResponse;
  loading: boolean;
  onRefresh: () => void;
  onClose: () => void;
  onNavigate: (path: string) => void;
  onDownload: (path: string, name: string) => void;
}

function Bar({ ratio, tone = "primary" }: { ratio: number; tone?: "primary" | "warning" }) {
  const pct = Math.max(1, Math.min(100, ratio * 100));
  return (
    <div className="h-1.5 w-full min-w-8 overflow-hidden rounded-full bg-midground/15">
      <div
        className={`h-full rounded-full ${tone === "primary" ? "bg-primary/80" : "bg-warning/80"}`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

function EntryRows({
  title,
  icon,
  entries,
  secondary,
  onOpen,
}: {
  title: string;
  icon: React.ReactNode;
  entries: FsAnalysisEntry[];
  secondary: "size" | "date";
  onOpen: (entry: FsAnalysisEntry) => void;
}) {
  if (entries.length === 0) return null;
  const top = entries[0]?.size ?? 0;
  return (
    <div className="min-w-0">
      <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-[0.08em] text-text-tertiary">
        {icon}
        {title}
      </div>
      <div className="flex flex-col gap-1">
        {entries.map((entry) => (
          <button
            key={entry.path}
            type="button"
            onClick={() => onOpen(entry)}
            title={entry.path}
            className="group flex min-w-0 items-center gap-2 rounded-sm px-1 py-1 text-left transition hover:bg-background/40"
          >
            <FileIcon className="h-3.5 w-3.5 shrink-0 text-text-tertiary" />
            <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">{entry.name}</span>
            <span className="shrink-0 text-xs tabular-nums text-text-secondary">
              {secondary === "date" && entry.mtime
                ? SHORT_DATE.format(entry.mtime * 1000)
                : formatBytes(entry.size)}
            </span>
            {secondary === "size" && (
              <span className="hidden w-16 shrink-0 sm:block">
                <Bar ratio={top > 0 ? (entry.size ?? 0) / top : 0} tone="warning" />
              </span>
            )}
          </button>
        ))}
      </div>
    </div>
  );
}

function ChildRows({
  children,
  onNavigate,
}: {
  children: FsAnalysisChild[];
  onNavigate: (path: string) => void;
}) {
  const dirs = children.filter((child) => child.is_directory).slice(0, 12);
  if (dirs.length === 0) return null;
  const top = dirs[0]?.bytes ?? 0;
  return (
    <div className="min-w-0">
      <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-[0.08em] text-text-tertiary">
        <FolderClosed className="h-3.5 w-3.5" />
        Space by folder
      </div>
      <div className="flex flex-col gap-1">
        {dirs.map((child) => (
          <button
            key={child.path}
            type="button"
            onClick={() => onNavigate(child.path)}
            title={`${child.path} — ${child.files} files, ${child.dirs} folders`}
            className="flex min-w-0 items-center gap-2 rounded-sm px-1 py-1 text-left transition hover:bg-background/40"
          >
            <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">{child.name}</span>
            <span className="shrink-0 text-xs tabular-nums text-text-secondary">{formatBytes(child.bytes)}</span>
            <span className="hidden w-24 shrink-0 sm:block">
              <Bar ratio={top > 0 ? child.bytes / top : 0} />
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

function TypeRows({ types }: { types: FsTypeStat[] }) {
  const ranked = types.slice(0, 8);
  if (ranked.length === 0) return null;
  const top = ranked[0]?.bytes ?? 0;
  return (
    <div className="min-w-0">
      <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-[0.08em] text-text-tertiary">
        <Database className="h-3.5 w-3.5" />
        Space by type
      </div>
      <div className="flex flex-col gap-1">
        {ranked.map((stat) => (
          <div key={stat.ext} className="flex min-w-0 items-center gap-2 px-1 py-1">
            <span className="w-16 shrink-0 truncate font-mono text-xs text-foreground">{stat.ext}</span>
            <span className="w-12 shrink-0 text-right text-xs tabular-nums text-text-secondary">{stat.count}</span>
            <span className="w-20 shrink-0 text-right text-xs tabular-nums text-text-secondary">
              {formatBytes(stat.bytes)}
            </span>
            <span className="hidden min-w-0 flex-1 sm:block">
              <Bar ratio={top > 0 ? stat.bytes / top : 0} />
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function FilesAnalysisCard({
  data,
  loading,
  onRefresh,
  onClose,
  onNavigate,
  onDownload,
}: FilesAnalysisCardProps) {
  const disk = data.disk;
  const openEntry = (entry: FsAnalysisEntry) => {
    if (entry.is_directory) onNavigate(entry.path);
    else onDownload(entry.path, entry.name);
  };

  return (
    <Card className="min-w-0 max-w-full">
      <CardContent className="flex flex-col gap-4 p-4">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="flex items-center gap-2 text-sm font-semibold uppercase tracking-[0.08em] text-foreground">
            <BarChart3 className="h-4 w-4 text-primary" />
            Storage analysis
          </span>
          <Badge tone="outline" className="max-w-[16rem] truncate font-mono text-xs" title={data.path}>
            {data.path}
          </Badge>
          {data.cached && (
            <Badge tone="outline" className="text-xs">
              cached
            </Badge>
          )}
          {data.truncated && (
            <Badge tone="warning" className="text-xs">
              partial scan
            </Badge>
          )}
          <span className="ml-auto flex items-center gap-1">
            <Button
              ghost
              size="icon"
              type="button"
              onClick={onRefresh}
              disabled={loading}
              aria-label="Refresh analysis"
            >
              {loading ? <Spinner /> : <RefreshCw />}
            </Button>
            <Button ghost size="icon" type="button" onClick={onClose} aria-label="Hide analysis">
              <X />
            </Button>
          </span>
        </div>

        {disk ? (
          <div className="flex min-w-0 flex-col gap-1.5">
            <div className="flex min-w-0 flex-wrap items-center justify-between gap-2 text-xs text-text-secondary">
              <span className="flex items-center gap-1.5 font-semibold uppercase tracking-[0.08em] text-text-tertiary">
                <HardDrive className="h-3.5 w-3.5" />
                Disk {disk.device}
              </span>
              <span className="tabular-nums">
                {formatBytes(disk.used)} used of {formatBytes(disk.total)} ({disk.percent}%) —{" "}
                {formatBytes(disk.free)} free
              </span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-midground/15">
              <div
                className={`h-full rounded-full ${disk.percent >= 90 ? "bg-destructive/80" : disk.percent >= 75 ? "bg-warning/80" : "bg-primary/80"}`}
                style={{ width: `${Math.min(100, disk.percent)}%` }}
              />
            </div>
          </div>
        ) : null}

        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-text-secondary">
          <span className="tabular-nums">{data.totals.files.toLocaleString()} files</span>
          <span className="tabular-nums">{data.totals.dirs.toLocaleString()} folders</span>
          <span className="tabular-nums font-semibold text-foreground">{formatBytes(data.totals.bytes)}</span>
          {data.totals.symlinks > 0 && <span className="tabular-nums">{data.totals.symlinks} links</span>}
          {data.totals.other_devices > 0 && (
            <span title={(data.mount_points ?? []).join("\n") || "Skipped mount points"}>
              {data.totals.other_devices} mounts skipped
            </span>
          )}
          <span className="ml-auto flex items-center gap-1 tabular-nums text-text-tertiary">
            <Clock className="h-3 w-3" />
            scanned in {(data.elapsed_ms / 1000).toFixed(1)}s
          </span>
        </div>

        <div className="grid min-w-0 gap-4 lg:grid-cols-2">
          <ChildRows children={data.children} onNavigate={onNavigate} />
          <TypeRows types={data.types} />
          <EntryRows
            title="Largest files"
            icon={<Database className="h-3.5 w-3.5" />}
            entries={data.largest.slice(0, 8)}
            secondary="size"
            onOpen={openEntry}
          />
          <EntryRows
            title="Recently modified"
            icon={<Clock className="h-3.5 w-3.5" />}
            entries={data.recent.slice(0, 8)}
            secondary="date"
            onOpen={openEntry}
          />
        </div>

        {data.recent.length > 0 && (
          <div className="text-[11px] text-text-tertiary">
            Last modified:{" "}
            {data.recent[0]?.mtime ? DATE_FORMAT.format(data.recent[0].mtime * 1000) : "-"} (
            {data.recent[0]?.name})
          </div>
        )}
      </CardContent>
    </Card>
  );
}
