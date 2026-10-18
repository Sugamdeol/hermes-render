import { useCallback, useEffect, useState } from "react";
import { FileIcon, Folder, Search } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@nous-research/ui/ui/components/dialog";
import { Input } from "@nous-research/ui/ui/components/input";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { searchFiles, type FsSearchMatch, type FsSearchResponse } from "@/lib/api-fs-analysis";
import { errorMessage } from "@/lib/api-error";
import { formatBytes } from "@/lib/fs-format";

const DATE_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

interface FilesSearchDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  path: string;
  onNavigateDirectory: (path: string) => void;
  onDownloadFile: (path: string, name: string) => void;
}

export function FilesSearchDialog({
  open,
  onOpenChange,
  path,
  onNavigateDirectory,
  onDownloadFile,
}: FilesSearchDialogProps) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<FsSearchResponse | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runSearch = useCallback(async () => {
    const needle = query.trim();
    if (needle.length < 2 || !path) return;
    setSearching(true);
    setError(null);
    try {
      const response = await searchFiles(path, needle);
      setResults(response);
    } catch (e) {
      setError(errorMessage(e));
      setResults(null);
    } finally {
      setSearching(false);
    }
  }, [query, path]);

  useEffect(() => {
    if (!open) {
      // Reset dialog state on close; the escape/backdrop exits bypass our own handlers.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setResults(null);
      setError(null);
      setSearching(false);
    }
  }, [open]);

  const openMatch = (match: FsSearchMatch) => {
    if (match.broken_link) return;
    if (match.is_directory) {
      onNavigateDirectory(match.path);
      onOpenChange(false);
    } else {
      onDownloadFile(match.path, match.name);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!searching) onOpenChange(next);
      }}
    >
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Search files</DialogTitle>
          <DialogDescription>
            Recursive name search under <span className="font-mono">{path || "..."}</span>. Use * for
            globs, plain text for substring matches.
          </DialogDescription>
        </DialogHeader>

        <form
          className="flex items-center gap-2 p-4 pt-0"
          onSubmit={(event) => {
            event.preventDefault();
            void runSearch();
          }}
        >
          <Input
            autoFocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="e.g. report, *.mp4, notebook"
            disabled={searching}
            className="min-w-0 flex-1 font-mono"
          />
          <Button type="submit" size="sm" outlined className="uppercase" disabled={searching || query.trim().length < 2}>
            {searching ? <Spinner /> : <Search />}
            Search
          </Button>
        </form>

        <div className="max-h-80 min-h-24 overflow-y-auto border-t border-border">
          {error && <div className="p-4 text-sm text-destructive">{error}</div>}
          {!error && !results && !searching && (
            <div className="p-6 text-center text-sm text-muted-foreground">
              Type a name fragment and press Search.
            </div>
          )}
          {searching && (
            <div className="flex items-center justify-center gap-2 p-6 text-sm text-muted-foreground">
              <Spinner />
              Searching...
            </div>
          )}
          {results && results.matches.length === 0 && !searching && (
            <div className="p-6 text-center text-sm text-muted-foreground">No matches under this folder.</div>
          )}
          {results &&
            results.matches.map((match) => (
              <button
                key={match.path}
                type="button"
                onClick={() => openMatch(match)}
                title={match.path}
                className="flex w-full min-w-0 items-center gap-2 border-b border-border/60 px-4 py-2 text-left text-sm transition last:border-b-0 hover:bg-background/40"
              >
                {match.is_directory ? (
                  <Folder className="h-4 w-4 shrink-0 text-warning" />
                ) : (
                  <FileIcon className="h-4 w-4 shrink-0 text-text-tertiary" />
                )}
                <span className="min-w-0 flex-1 truncate font-mono text-foreground">{match.name}</span>
                <span className="shrink-0 text-xs tabular-nums text-text-secondary">
                  {match.is_directory ? "folder" : formatBytes(match.size)}
                </span>
                <span className="hidden w-36 shrink-0 truncate text-right text-xs text-text-tertiary sm:block">
                  {match.mtime ? DATE_FORMAT.format(match.mtime * 1000) : "-"}
                </span>
              </button>
            ))}
        </div>

        <DialogFooter className="items-center justify-between gap-2 sm:justify-between">
          <span className="min-w-0 truncate text-xs text-text-tertiary">
            {results
              ? `${results.matches.length} match${results.matches.length === 1 ? "" : "es"} in ${(results.elapsed_ms / 1000).toFixed(1)}s`
              : "Results open folders or download files."}
          </span>
          {results?.truncated && (
            <Badge tone="warning" className="text-xs">
              search budget reached — results partial
            </Badge>
          )}
          <Button type="button" outlined size="sm" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
