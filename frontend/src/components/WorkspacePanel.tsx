import { useEffect, useMemo, useState } from "react";

import { useWorkspace, useWorkspaceFile, useWriteWorkspaceFile } from "../api/workspace";

export function WorkspacePanel({ agentId }: { agentId: string }) {
  const [path, setPath] = useState(".");
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  const { data: listing } = useWorkspace(agentId, path);
  const { data: file } = useWorkspaceFile(agentId, selectedFile);
  const writeFile = useWriteWorkspaceFile(agentId, path);

  useEffect(() => {
    if (file?.content !== undefined) {
      setDraft(file.content);
    }
  }, [file]);

  const directories = useMemo(
    () => (listing?.entries ?? []).filter((entry) => entry.is_dir),
    [listing?.entries],
  );
  const files = useMemo(
    () => (listing?.entries ?? []).filter((entry) => !entry.is_dir),
    [listing?.entries],
  );

  return (
    <section className="grid gap-6 xl:grid-cols-[0.42fr_0.58fr]">
      <div className="panel-frame p-6">
        <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
          <div>
            <p className="panel-label">Workspace</p>
            <p className="mt-2 text-lg text-[var(--text-display)]">{path}</p>
          </div>
          <p className="panel-label">{listing?.size ?? 0} bytes</p>
        </div>

        {path !== "." ? (
          <button
            type="button"
            className="panel-button-secondary mt-4 w-full"
            onClick={() => {
              const parts = path.split("/").filter(Boolean);
              setPath(parts.length <= 1 ? "." : parts.slice(0, -1).join("/"));
            }}
          >
            Go up
          </button>
        ) : null}

        <div className="mt-4 space-y-2">
          {directories.map((entry) => (
            <button
              key={entry.path}
              type="button"
              className="block w-full border-b border-[var(--border)] py-3 text-left"
              onClick={() => {
                setPath(entry.path);
                setSelectedFile(null);
              }}
            >
              <p className="panel-label">Dir</p>
              <p className="mt-1 text-sm text-[var(--text-display)]">{entry.name}</p>
            </button>
          ))}
          {files.map((entry) => (
            <button
              key={entry.path}
              type="button"
              className="block w-full border-b border-[var(--border)] py-3 text-left"
              onClick={() => setSelectedFile(entry.path)}
            >
              <p className="panel-label">File</p>
              <p className="mt-1 text-sm text-[var(--text-display)]">{entry.name}</p>
            </button>
          ))}
        </div>
      </div>

      <div className="panel-frame p-6">
        <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
          <div>
            <p className="panel-label">Editor</p>
            <p className="mt-2 text-lg text-[var(--text-display)]">
              {selectedFile ?? "Select a file"}
            </p>
          </div>
          {selectedFile ? (
            <button
              type="button"
              className="panel-button-primary"
              onClick={() => selectedFile && writeFile.mutate({ filePath: selectedFile, content: draft })}
            >
              Save file
            </button>
          ) : null}
        </div>
        <textarea
          className="mt-4 min-h-[360px]"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Workspace file contents"
          disabled={!selectedFile}
        />
      </div>
    </section>
  );
}

