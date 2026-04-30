import { useMutation, useQuery } from "@tanstack/react-query";

import { apiClient } from "./client";
import type {
  InstanceBackupCreateRequest,
  InstanceBackupRestoreResult,
  InstanceBackupValidation,
} from "../types/api";

function downloadBlob(blob: Blob, filename: string) {
  const url = window.URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => {
    window.URL.revokeObjectURL(url);
  }, 1000);
}

function parseDispositionFilename(value: string | undefined) {
  if (!value) {
    return null;
  }
  const match = /filename=\"?([^\";]+)\"?/i.exec(value);
  return match?.[1] ?? null;
}

export function useCreateInstanceBackup() {
  return useMutation({
    mutationFn: async (payload: InstanceBackupCreateRequest) => {
      const response = await apiClient.post("/backup/create", payload, {
        responseType: "blob",
      });
      const filename =
        parseDispositionFilename(response.headers["content-disposition"]) ?? "hermeshq-backup.zip";
      downloadBlob(response.data, filename);
      return {
        filename,
        blob: response.data as Blob,
      };
    },
  });
}

export function useValidateInstanceBackup() {
  return useMutation({
    mutationFn: async ({ file, passphrase }: { file: File; passphrase?: string }) => {
      const formData = new FormData();
      formData.append("file", file);
      if (passphrase) {
        formData.append("passphrase", passphrase);
      }
      const { data } = await apiClient.post<InstanceBackupValidation>("/backup/validate", formData, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      return data;
    },
  });
}

export function useRestoreInstanceBackup() {
  return useMutation({
    mutationFn: async ({
      file,
      passphrase,
      mode,
    }: {
      file: File;
      passphrase: string;
      mode: "replace" | "merge";
    }) => {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("passphrase", passphrase);
      formData.append("mode", mode);
      const { data } = await apiClient.post<InstanceBackupRestoreResult>("/backup/restore", formData, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      return data;
    },
  });
}

export function useRestoreInstanceBackupJob(jobId: string | null) {
  return useQuery<InstanceBackupRestoreResult>({
    queryKey: ["backup-restore-job", jobId],
    enabled: Boolean(jobId),
    queryFn: async () => {
      const { data } = await apiClient.get<InstanceBackupRestoreResult>(`/backup/restore-jobs/${jobId}`);
      return data;
    },
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "queued" || status === "running" ? 1500 : false;
    },
  });
}
