// SPDX-License-Identifier: AGPL-3.0-only
// Cloud jobs: run Studio training on cloud GPUs (SkyPilot) and watch it from this UI.

import { usePlatformStore } from "@/config/env";
import { authFetch, isAccountOwner } from "@/features/auth";
import { prepareHfTokenForUse } from "@/features/hf-auth";
import { getHfToken } from "@/features/hub";
import type { TaskSchedule } from "@/features/scheduled-tasks/schedule";
import { buildTrainingStartPayload } from "@/features/training/api/mappers";
import { validateTrainingConfig } from "@/features/training/lib/validation";
import { useTrainingConfigStore } from "@/features/training/stores/training-config-store";
import { getCachedSystemInfo } from "@/hooks/use-system";
import { translate } from "@/i18n";
import { readFastApiError } from "@/lib/format-fastapi-error";

const API = "/api/cloud-jobs";

export type Infra = "gcp" | "aws" | "kubernetes";
export type Launcher = "local" | "skypilot";
export type CloudJobState = "scheduled" | "launching" | "running" | "succeeded" | "failed" | "cancelled";

export interface CloudResources {
  launcher: Launcher;
  infra: Infra;
  regions: string[];
  accelerator: string;
  use_spot: boolean;
  disk_size: number;
  max_hours?: number;
}

export interface CloudJob {
  id: string;
  name: string | null;
  state: CloudJobState;
  resources: CloudResources;
  schedule: TaskSchedule | null;
  timezone: string | null;
  request: { model_name?: string; hf_dataset?: string } & Record<string, unknown>;
  folder: string;
  created_at: number;
  next_run_at: number | null;
  started_at: number | null;
  ended_at: number | null;
  external_id: string | null;
  parent_id: string | null;
  error: string | null;
  attempts: number | null;
  live?: {
    step: number | null;
    total_steps: number | null;
    loss: number | null;
    epoch: number | null;
    eta_seconds: number | null;
    status_message: string | null;
    error: string | null;
  };
}

export interface GpuRegion {
  region: string;
  price: number;
  spot_price: number | null;
  instance_type: string;
}
export interface GpuOption {
  accelerator: string;
  memory_gb: number;
  vcpus: number | null;
  ram_gb: number | null;
  regions: GpuRegion[];
  price: number;
  spot_price: number | null;
  hourly: number;
  region: string;
  instance_type: string;
  fits: boolean;
}

export interface GpuEstimate {
  required_gb: number | null;
  options: GpuOption[];
  suggested: string | null;
  other_counts?: Record<string, number[]>; // GPUs not sold with the asked count per machine -> counts they are sold in
  headroom: number;
  catalog_error: string | null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await authFetch(`${API}${path}`, {
    ...init,
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
  });
  if (!res.ok) throw new Error(await readFastApiError(res));
  return (res.status === 204 ? undefined : await res.json()) as T;
}

const jobPath = (id: string, suffix = "") => `/${encodeURIComponent(id)}${suffix}`;

export const listCloudJobs = () => request<CloudJob[]>("");
export const getCloudJob = (id: string) => request<CloudJob>(jobPath(id));
export const cancelCloudJob = (id: string) => request<CloudJob>(jobPath(id, "/cancel"), { method: "POST" });
export const rerunCloudJob = (id: string) => request<CloudJob>(jobPath(id, "/rerun"), { method: "POST" });
export const getCloudJobLogs = (id: string, tail = 300) =>
  request<{ logs: string }>(jobPath(id, `/logs?tail=${tail}`));

/** The same validated request the Train button would send to /api/train/start, built from the current config. */
export async function buildCloudTrainingRequest(): Promise<Record<string, unknown> | null> {
  const config = useTrainingConfigStore.getState();
  const validation = validateTrainingConfig(config, usePlatformStore.getState().deviceType, isAccountOwner());
  if (!validation.ok) throw new Error(translate(validation.errorKey));
  const prepared = await prepareHfTokenForUse(getHfToken());
  if (!prepared.proceed) return null;
  // Cloud GPUs train on a GPU even when this server has none.
  return buildTrainingStartPayload(config, prepared.token, getCachedSystemInfo(), false) as unknown as Record<
    string,
    unknown
  >;
}

/** The current config as a request, without validation or prompts: only for live GPU estimates. */
export function currentRequestForEstimate(): Record<string, unknown> {
  return buildTrainingStartPayload(useTrainingConfigStore.getState(), null, getCachedSystemInfo(), false) as unknown as Record<
    string,
    unknown
  >;
}

export const estimateCloudGpu = (body: {
  request: Record<string, unknown>;
  infra: Infra;
  use_spot: boolean;
  gpu_count: number;
}) =>
  request<GpuEstimate>("/estimate", { method: "POST", body: JSON.stringify(body) });

export const createCloudJob = (body: {
  name?: string;
  request: Record<string, unknown>;
  resources: CloudResources;
  schedule?: TaskSchedule | null;
  timezone: string;
}) => request<CloudJob>("", { method: "POST", body: JSON.stringify(body) });
