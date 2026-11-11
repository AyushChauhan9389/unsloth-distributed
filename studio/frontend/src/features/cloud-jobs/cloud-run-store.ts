// SPDX-License-Identifier: AGPL-3.0-only
// Cloud run settings edited in the Train wizard's cloud mode; read by the run preview and the start button.

import { type TaskSchedule, browserTimeZone } from "@/features/scheduled-tasks/schedule";
import { create } from "zustand";
import {
  type CloudJob,
  type GpuEstimate,
  type Infra,
  type Launcher,
  buildCloudTrainingRequest,
  createCloudJob,
} from "./api";

export type When = "now" | "once" | "hourly" | "daily" | "weekly";

interface CloudRunSettings {
  name: string;
  launcher: Launcher;
  infra: Infra;
  useSpot: boolean;
  gpu: string;
  gpuCount: number;
  regions: string[]; // tried in this order; empty = any region with capacity
  diskSize: number;
  maxHours: number;
  when: When;
  at: string;
  time: string;
  hours: number;
  days: number[];
  estimate: GpuEstimate | null;
  estimating: boolean;
}

interface CloudRunState extends CloudRunSettings {
  set: (patch: Partial<CloudRunSettings>) => void;
}

export const useCloudRunStore = create<CloudRunState>((set) => ({
  name: "",
  launcher: "skypilot",
  infra: "gcp",
  useSpot: true,
  gpu: "",
  gpuCount: 1,
  regions: [],
  diskSize: 200,
  maxHours: 12,
  when: "now",
  at: "",
  time: "02:00",
  hours: 24,
  days: [1],
  estimate: null,
  estimating: false,
  set: (patch) => set(patch),
}));

export function buildSchedule(s: Pick<CloudRunSettings, "when" | "at" | "time" | "hours" | "days">): TaskSchedule | null {
  if (s.when === "now") return null;
  if (s.when === "once") return { kind: "once", at: s.at };
  if (s.when === "hourly") return { kind: "hourly", hours: s.hours };
  if (s.when === "daily") return { kind: "daily", time: s.time };
  return { kind: "weekly", time: s.time, days: s.days };
}

/** Why the cloud run cannot be submitted yet, or null when it can. */
export function cloudRunBlocker(s: CloudRunSettings): string | null {
  if (s.launcher === "skypilot" && !s.gpu) return "Pick a GPU";
  if (s.when === "once" && !s.at) return "Pick a start time";
  if (s.when === "weekly" && s.days.length === 0) return "Pick at least one day";
  return null;
}

/** Validates the wizard config (like the Train button) and submits it as a cloud job. Null when the user backed out. */
export async function submitCloudRun(): Promise<CloudJob | null> {
  const s = useCloudRunStore.getState();
  const blocker = cloudRunBlocker(s);
  if (blocker) throw new Error(blocker);
  const request = await buildCloudTrainingRequest();
  if (!request) return null;
  return createCloudJob({
    name: s.name.trim() || undefined,
    request,
    resources: {
      launcher: s.launcher,
      infra: s.infra,
      regions: s.regions,
      accelerator: `${s.gpu}:${s.gpuCount}`,
      use_spot: s.useSpot,
      disk_size: s.diskSize,
      max_hours: s.maxHours,
    },
    schedule: buildSchedule(s),
    timezone: browserTimeZone(),
  });
}
