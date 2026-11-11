// SPDX-License-Identifier: AGPL-3.0-only
// Cloud jobs: every job (scheduled, running, finished) and, for the selected one, Studio's own
// progress/loss views (refreshed live), logs, cancel and rerun.

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { HistoricalTrainingView } from "@/features/studio/historical-training-view";
import { toast } from "@/lib/toast";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { type ReactElement, useCallback, useEffect, useState } from "react";
import {
  type CloudJob,
  type CloudJobState,
  cancelCloudJob,
  getCloudJobLogs,
  listCloudJobs,
  rerunCloudJob,
} from "./api";

const LIST_REFRESH_MS = 5000;
const LOGS_REFRESH_MS = 10000;
const ACTIVE: CloudJobState[] = ["scheduled", "launching", "running"];
const STATE_VARIANT: Record<CloudJobState, "default" | "secondary" | "destructive" | "outline"> = {
  scheduled: "outline",
  launching: "secondary",
  running: "default",
  succeeded: "secondary",
  failed: "destructive",
  cancelled: "outline",
};

function when(ms: number | null): string {
  return ms ? new Date(ms).toLocaleString() : "-";
}

function JobRow({ job, selected, onSelect }: { job: CloudJob; selected: boolean; onSelect: () => void }) {
  const live = job.live;
  const progress = live?.step != null && live.total_steps ? `${live.step}/${live.total_steps}` : "";
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`grid w-full grid-cols-[1fr_auto] gap-1 rounded-lg border p-3 text-left text-sm ${
        selected ? "border-primary bg-accent" : "hover:bg-accent/50"
      }`}
    >
      <span className="truncate font-medium">{job.name ?? job.request.model_name ?? job.id}</span>
      <Badge variant={STATE_VARIANT[job.state]}>{job.state}</Badge>
      <span className="truncate text-muted-foreground">
        {job.resources.launcher === "local" ? "this machine" : `${job.resources.infra} · ${job.resources.accelerator}`}
        {job.resources.use_spot && job.resources.launcher !== "local" ? " · spot" : ""}
      </span>
      <span className="text-muted-foreground tabular-nums">
        {progress}
        {live?.loss != null ? ` · loss ${live.loss.toFixed(3)}` : ""}
      </span>
      {job.state === "scheduled" && (
        <span className="col-span-2 text-muted-foreground">
          next run {when(job.next_run_at)}
          {job.schedule ? ` · repeats (${job.schedule.kind})` : ""}
        </span>
      )}
    </button>
  );
}

function JobLogs({ job }: { job: CloudJob }) {
  const [logs, setLogs] = useState("");
  useEffect(() => {
    let alive = true;
    const load = () =>
      getCloudJobLogs(job.id)
        .then((r) => alive && setLogs(r.logs))
        .catch(() => undefined);
    void load();
    if (!ACTIVE.includes(job.state)) return () => void (alive = false);
    const id = window.setInterval(load, LOGS_REFRESH_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [job.id, job.state]);
  return (
    <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-lg border bg-muted/40 p-3 font-mono text-xs leading-relaxed">
      {logs || "No logs yet."}
    </pre>
  );
}

function JobDetail({ job, onChanged }: { job: CloudJob; onChanged: () => void }) {
  const act = (fn: (id: string) => Promise<unknown>, done: string) => () =>
    fn(job.id)
      .then(() => {
        toast.success(done);
        onChanged();
      })
      .catch((e) => toast.error(e instanceof Error ? e.message : String(e)));
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">{job.name ?? job.request.model_name}</h2>
          <p className="text-sm text-muted-foreground">
            {job.request.model_name} · {job.request.hf_dataset || "uploaded dataset"} · created {when(job.created_at)}
            {job.started_at ? ` · started ${when(job.started_at)}` : ""}
            {job.ended_at ? ` · ended ${when(job.ended_at)}` : ""}
          </p>
          {job.live?.status_message && ACTIVE.includes(job.state) && (
            <p className="text-sm">{job.live.status_message}</p>
          )}
          {job.error && <p className="text-sm text-destructive">{job.error}</p>}
        </div>
        <div className="flex gap-2">
          {ACTIVE.includes(job.state) && (
            <Button variant="outline" size="sm" onClick={act(cancelCloudJob, "Cancelling job")}>
              Cancel
            </Button>
          )}
          <Button variant="outline" size="sm" onClick={act(rerunCloudJob, "Rerun queued")}>
            Rerun
          </Button>
        </div>
      </div>
      {job.started_at ? <HistoricalTrainingView runId={job.id} live /> : null}
      <div>
        <h3 className="mb-2 text-sm font-medium">Logs</h3>
        <JobLogs job={job} />
      </div>
    </div>
  );
}

export function CloudJobsPage(): ReactElement {
  const navigate = useNavigate();
  const { job: selectedId } = useSearch({ from: "/cloud-jobs" });
  const [jobs, setJobs] = useState<CloudJob[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    listCloudJobs()
      .then((j) => {
        setJobs(j);
        setError(null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, LIST_REFRESH_MS);
    return () => window.clearInterval(id);
  }, [refresh]);

  const selected = jobs?.find((j) => j.id === selectedId) ?? jobs?.[0] ?? null;
  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-4 p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Cloud jobs</h1>
          <p className="text-sm text-muted-foreground">
            Training runs on cloud GPUs or this machine, now or on a schedule.
          </p>
        </div>
        <Button onClick={() => navigate({ to: "/studio", search: { cloud: true } })}>New cloud run</Button>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
      {jobs && jobs.length === 0 && <p className="text-sm text-muted-foreground">No cloud jobs yet.</p>}
      {jobs && jobs.length > 0 && (
        <div className="grid gap-6 lg:grid-cols-[22rem_minmax(0,1fr)]">
          <div className="flex flex-col gap-2">
            {jobs.map((j) => (
              <JobRow
                key={j.id}
                job={j}
                selected={j.id === selected?.id}
                onSelect={() => void navigate({ to: "/cloud-jobs", search: { job: j.id } })}
              />
            ))}
          </div>
          {selected && <JobDetail key={selected.id} job={selected} onChanged={refresh} />}
        </div>
      )}
    </div>
  );
}
