// SPDX-License-Identifier: AGPL-3.0-only
// "Run on cloud": pick where/when to run the current training config; suggests the cheapest GPU that fits.

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { type TaskSchedule, browserTimeZone } from "@/features/scheduled-tasks/schedule";
import { toast } from "@/lib/toast";
import { useNavigate } from "@tanstack/react-router";
import { type ReactElement, type ReactNode, useEffect, useState } from "react";
import {
  type GpuEstimate,
  type Infra,
  type Launcher,
  buildCloudTrainingRequest,
  createCloudJob,
  estimateCloudGpu,
} from "./api";

type When = "now" | "once" | "hourly" | "daily" | "weekly";
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <div className="flex min-w-0 max-w-[65%] items-center gap-2">{children}</div>
    </div>
  );
}

export function buildSchedule(when: When, at: string, time: string, hours: number, days: number[]): TaskSchedule | null {
  if (when === "now") return null;
  if (when === "once") return { kind: "once", at };
  if (when === "hourly") return { kind: "hourly", hours };
  if (when === "daily") return { kind: "daily", time };
  return { kind: "weekly", time, days };
}

export function CloudRunDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }): ReactElement {
  const navigate = useNavigate();
  const [request, setRequest] = useState<Record<string, unknown> | null>(null);
  const [prepError, setPrepError] = useState<string | null>(null);
  const [launcher, setLauncher] = useState<Launcher>("skypilot");
  const [infra, setInfra] = useState<Infra>("gcp");
  const [useSpot, setUseSpot] = useState(true);
  const [regions, setRegions] = useState("");
  const [diskSize, setDiskSize] = useState(200);
  const [estimate, setEstimate] = useState<GpuEstimate | null>(null);
  const [estimating, setEstimating] = useState(false);
  const [gpu, setGpu] = useState("");
  const [gpuCount, setGpuCount] = useState(1);
  const [name, setName] = useState("");
  const [when, setWhen] = useState<When>("now");
  const [at, setAt] = useState("");
  const [time, setTime] = useState("02:00");
  const [hours, setHours] = useState(24);
  const [days, setDays] = useState<number[]>([1]);
  const [busy, setBusy] = useState(false);

  // Validate the current training config and build the same request the Train button would send.
  useEffect(() => {
    if (!open) return;
    setRequest(null);
    setPrepError(null);
    buildCloudTrainingRequest()
      .then((r) => (r ? setRequest(r) : onOpenChange(false)))
      .catch((e) => setPrepError(e instanceof Error ? e.message : String(e)));
  }, [open, onOpenChange]);

  // Re-estimate when the request, cloud or spot choice changes; pre-select the suggested GPU.
  useEffect(() => {
    if (!request || launcher !== "skypilot") return;
    let cancelled = false;
    setEstimating(true);
    estimateCloudGpu({ request, infra, use_spot: useSpot })
      .then((e) => {
        if (cancelled) return;
        setEstimate(e);
        if (e.suggested) setGpu(e.suggested);
      })
      .catch((e) => !cancelled && toast.error(e instanceof Error ? e.message : String(e)))
      .finally(() => !cancelled && setEstimating(false));
    return () => {
      cancelled = true;
    };
  }, [request, launcher, infra, useSpot]);

  const schedule = buildSchedule(when, at, time, hours, days);
  const scheduleOk = (when !== "once" || at !== "") && (when !== "weekly" || days.length > 0);
  const gpuOk = launcher === "local" || gpu.trim() !== "";
  const canSubmit = !!request && !busy && scheduleOk && gpuOk;

  const submit = async () => {
    if (!request) return;
    setBusy(true);
    try {
      const job = await createCloudJob({
        name: name.trim() || undefined,
        request,
        resources: {
          launcher,
          infra,
          regions: regions.split(",").map((r) => r.trim()).filter(Boolean),
          accelerator: `${gpu.trim()}:${gpuCount}`,
          use_spot: useSpot,
          disk_size: diskSize,
        },
        schedule,
        timezone: browserTimeZone(),
      });
      toast.success(schedule ? "Cloud job scheduled" : "Cloud job submitted");
      onOpenChange(false);
      void navigate({ to: "/cloud-jobs", search: { job: job.id } });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const selected = estimate?.options.find((o) => o.accelerator === gpu);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Run on cloud</DialogTitle>
        </DialogHeader>
        {prepError ? (
          <p className="text-sm text-destructive">{prepError}</p>
        ) : !request ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner className="size-4" /> Checking the training config...
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            <Row label="Name">
              <Input value={name} placeholder={String(request.model_name ?? "")} onChange={(e) => setName(e.target.value)} />
            </Row>
            <Row label="Run on">
              <Select value={launcher} onValueChange={(v) => setLauncher(v as Launcher)}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="skypilot">Cloud GPU (SkyPilot)</SelectItem>
                  <SelectItem value="local">This machine's GPU</SelectItem>
                </SelectContent>
              </Select>
            </Row>
            {launcher === "skypilot" && (
              <>
                <Row label="Cloud">
                  <Select value={infra} onValueChange={(v) => setInfra(v as Infra)}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="gcp">Google Cloud</SelectItem>
                      <SelectItem value="aws">AWS</SelectItem>
                      <SelectItem value="kubernetes">Kubernetes</SelectItem>
                    </SelectContent>
                  </Select>
                </Row>
                <Row label="Spot (cheaper, can be reclaimed; resumes from checkpoint)">
                  <Switch checked={useSpot} onCheckedChange={setUseSpot} />
                </Row>
                <div className="rounded-lg border p-3 text-sm">
                  <div className="mb-2 flex items-center justify-between">
                    <span className="font-medium">GPU</span>
                    {estimating ? (
                      <Spinner className="size-4" />
                    ) : estimate?.required_gb != null ? (
                      <span className="text-muted-foreground">
                        Needs ~{estimate.required_gb.toFixed(1)} GB VRAM (+{Math.round((estimate.headroom - 1) * 100)}% headroom)
                      </span>
                    ) : (
                      <span className="text-muted-foreground">VRAM estimate unavailable</span>
                    )}
                  </div>
                  {estimate?.catalog_error && <p className="mb-2 text-status-warning">{estimate.catalog_error}</p>}
                  {estimate && estimate.options.length > 0 ? (
                    <div className="flex max-h-48 flex-col gap-1 overflow-y-auto">
                      {estimate.options.map((o) => (
                        <button
                          key={o.accelerator}
                          type="button"
                          onClick={() => setGpu(o.accelerator)}
                          className={`flex items-center justify-between rounded-md px-2 py-1 text-left ${
                            gpu === o.accelerator ? "bg-accent" : "hover:bg-accent/50"
                          } ${o.fits ? "" : "opacity-50"}`}
                        >
                          <span>
                            {o.accelerator} · {o.memory_gb} GB {o.accelerator === estimate.suggested && "· suggested"}
                            {!o.fits && " · too small"}
                          </span>
                          <span className="tabular-nums">${o.hourly.toFixed(2)}/hr</span>
                        </button>
                      ))}
                    </div>
                  ) : (
                    <Input value={gpu} placeholder="e.g. H100" onChange={(e) => setGpu(e.target.value)} />
                  )}
                  <div className="mt-2 flex items-center justify-between">
                    <span className="text-muted-foreground">GPUs per job</span>
                    <Input
                      type="number"
                      min={1}
                      max={8}
                      className="w-20"
                      value={gpuCount}
                      onChange={(e) => setGpuCount(Math.max(1, Number(e.target.value) || 1))}
                    />
                  </div>
                  {selected && (
                    <p className="mt-2 text-muted-foreground">
                      {selected.instance_type} in {selected.region} · ~${(selected.hourly * gpuCount).toFixed(2)}/hr
                    </p>
                  )}
                </div>
                <Row label="Regions (optional, in order)">
                  <Input value={regions} placeholder="asia-south2, us-east1" onChange={(e) => setRegions(e.target.value)} />
                </Row>
                <Row label="Disk (GB)">
                  <Input type="number" min={50} max={4000} className="w-24" value={diskSize}
                    onChange={(e) => setDiskSize(Number(e.target.value) || 200)} />
                </Row>
              </>
            )}
            <Row label="When">
              <Select value={when} onValueChange={(v) => setWhen(v as When)}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="now">Now</SelectItem>
                  <SelectItem value="once">Once at...</SelectItem>
                  <SelectItem value="hourly">Every N hours</SelectItem>
                  <SelectItem value="daily">Daily</SelectItem>
                  <SelectItem value="weekly">Weekly</SelectItem>
                </SelectContent>
              </Select>
            </Row>
            {when === "once" && (
              <Row label="Start at">
                <Input type="datetime-local" value={at} onChange={(e) => setAt(e.target.value)} />
              </Row>
            )}
            {when === "hourly" && (
              <Row label="Every (hours)">
                <Input type="number" min={1} max={24} className="w-20" value={hours}
                  onChange={(e) => setHours(Math.min(24, Math.max(1, Number(e.target.value) || 1)))} />
              </Row>
            )}
            {(when === "daily" || when === "weekly") && (
              <Row label="Time">
                <Input type="time" value={time} onChange={(e) => setTime(e.target.value)} />
              </Row>
            )}
            {when === "weekly" && (
              <Row label="Days">
                {WEEKDAYS.map((d, i) => (
                  <Button key={d} type="button" size="sm" variant={days.includes(i + 1) ? "default" : "outline"}
                    onClick={() =>
                      setDays((cur) => (cur.includes(i + 1) ? cur.filter((x) => x !== i + 1) : [...cur, i + 1]).sort())
                    }>
                    {d}
                  </Button>
                ))}
              </Row>
            )}
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button disabled={!canSubmit} onClick={() => void submit()}>
            {busy && <Spinner className="size-4" />}
            {when === "now" ? "Start on cloud" : "Schedule"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
