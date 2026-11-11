// SPDX-License-Identifier: AGPL-3.0-only
// Cloud settings section of the Train wizard (cloud mode): where, which GPU, pricing, regions, limits, when.

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { useTrainingConfigStore } from "@/features/training/stores/training-config-store";
import { cn } from "@/lib/utils";
import { type ReactElement, type ReactNode, useEffect } from "react";
import { type GpuOption, type Infra, type Launcher, currentRequestForEstimate, estimateCloudGpu } from "./api";
import { type When, useCloudRunStore } from "./cloud-run-store";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const ESTIMATE_DEBOUNCE_MS = 600;

const money = (n: number | null | undefined) => (n == null ? "–" : `$${n.toFixed(2)}`);

export const hourlyFor = (o: GpuOption, useSpot: boolean, region?: string) => {
  const r = region ? o.regions.find((x) => x.region === region) : undefined;
  const price = r ? r.price : o.price;
  const spot = r ? r.spot_price : o.spot_price;
  return useSpot && spot != null ? spot : price;
};

function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
      <div className="flex min-w-0 flex-col">
        <span className="font-medium">{label}</span>
        {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
      </div>
      <div className="flex min-w-0 flex-wrap items-center justify-end gap-2">{children}</div>
    </div>
  );
}

function Choice<T extends string | number | boolean>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1">
      {options.map((o) => (
        <Button
          key={String(o.value)}
          type="button"
          size="sm"
          variant={o.value === value ? "default" : "outline"}
          className="rounded-full"
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </Button>
      ))}
    </div>
  );
}

export function CloudSettingsPanel(): ReactElement {
  const s = useCloudRunStore();
  const configVersion = useTrainingConfigStore((c) => c); // any config change re-estimates the GPU need

  // Live VRAM estimate + GPU catalog for the chosen cloud; pre-picks the cheapest GPU that fits.
  useEffect(() => {
    if (s.launcher !== "skypilot") return;
    let cancelled = false;
    const id = window.setTimeout(() => {
      useCloudRunStore.getState().set({ estimating: true });
      estimateCloudGpu({ request: currentRequestForEstimate(), infra: s.infra, use_spot: s.useSpot })
        .then((e) => {
          if (cancelled) return;
          const cur = useCloudRunStore.getState();
          const keep = e.options.some((o) => o.accelerator === cur.gpu);
          cur.set({ estimate: e, gpu: keep ? cur.gpu : (e.suggested ?? "") });
        })
        .catch((err) => {
          if (cancelled) return;
          useCloudRunStore.getState().set({
            estimate: { required_gb: null, options: [], suggested: null, headroom: 1, catalog_error: String(err?.message ?? err) },
          });
        })
        .finally(() => !cancelled && useCloudRunStore.getState().set({ estimating: false }));
    }, ESTIMATE_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(id);
    };
  }, [s.launcher, s.infra, s.useSpot, configVersion]);

  const selected = s.estimate?.options.find((o) => o.accelerator === s.gpu);
  const toggleRegion = (region: string) =>
    s.set({ regions: s.regions.includes(region) ? s.regions.filter((r) => r !== region) : [...s.regions, region] });

  return (
    <div className="flex flex-col gap-5">
      <Row label="Run on">
        <Choice<Launcher>
          value={s.launcher}
          onChange={(launcher) => s.set({ launcher })}
          options={[
            { value: "skypilot", label: "Cloud GPU" },
            { value: "local", label: "This machine" },
          ]}
        />
      </Row>

      {s.launcher === "skypilot" && (
        <>
          <Row label="Cloud">
            <Choice<Infra>
              value={s.infra}
              onChange={(infra) => s.set({ infra, regions: [] })}
              options={[
                { value: "gcp", label: "Google Cloud" },
                { value: "aws", label: "AWS" },
                { value: "kubernetes", label: "Kubernetes" },
              ]}
            />
          </Row>

          <Row label="Pricing" hint="Spot is cheaper but can be reclaimed; the job resumes from its last checkpoint.">
            <Choice<boolean>
              value={s.useSpot}
              onChange={(useSpot) => s.set({ useSpot })}
              options={[
                { value: true, label: "Spot" },
                { value: false, label: "On-demand" },
              ]}
            />
          </Row>

          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span className="font-medium">GPU</span>
              {s.estimating ? (
                <Spinner className="size-4" />
              ) : s.estimate?.required_gb != null ? (
                <span className="text-xs text-muted-foreground">
                  This run needs ~{s.estimate.required_gb.toFixed(1)} GB VRAM (+
                  {Math.round((s.estimate.headroom - 1) * 100)}% headroom)
                </span>
              ) : null}
            </div>
            {s.estimate?.catalog_error && <p className="text-xs text-destructive">{s.estimate.catalog_error}</p>}
            {s.estimate && s.estimate.options.length > 0 ? (
              <div className="overflow-x-auto rounded-lg border">
                <table className="w-full text-sm">
                  <thead className="text-xs text-muted-foreground">
                    <tr className="border-b text-left">
                      <th className="px-3 py-2 font-medium">GPU</th>
                      <th className="px-3 py-2 font-medium">VRAM</th>
                      <th className="px-3 py-2 font-medium">vCPU / RAM</th>
                      <th className="px-3 py-2 text-right font-medium">On-demand</th>
                      <th className="px-3 py-2 text-right font-medium">Spot</th>
                    </tr>
                  </thead>
                  <tbody>
                    {s.estimate.options.map((o) => (
                      <tr
                        key={o.accelerator}
                        onClick={() => s.set({ gpu: o.accelerator, regions: [] })}
                        className={cn(
                          "cursor-pointer border-b last:border-0",
                          o.accelerator === s.gpu ? "bg-accent" : "hover:bg-accent/50",
                          !o.fits && "opacity-50",
                        )}
                      >
                        <td className="px-3 py-2 font-medium">
                          {o.accelerator}
                          {o.accelerator === s.estimate?.suggested && (
                            <span className="ml-2 text-xs text-primary">suggested</span>
                          )}
                          {!o.fits && <span className="ml-2 text-xs text-muted-foreground">too small</span>}
                        </td>
                        <td className="px-3 py-2 tabular-nums">{o.memory_gb} GB</td>
                        <td className="px-3 py-2 tabular-nums">
                          {o.vcpus ?? "–"} / {o.ram_gb != null ? `${Math.round(o.ram_gb)} GB` : "–"}
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums">{money(o.price)}/h</td>
                        <td className="px-3 py-2 text-right tabular-nums">{money(o.spot_price)}/h</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              !s.estimating && (
                <Input value={s.gpu} placeholder="GPU name, e.g. H100" onChange={(e) => s.set({ gpu: e.target.value })} />
              )
            )}
          </div>

          <Row label="GPUs per machine" hint="More than one trains distributed across the machine's GPUs.">
            <Choice<number>
              value={s.gpuCount}
              onChange={(gpuCount) => s.set({ gpuCount })}
              options={[1, 2, 4, 8].map((n) => ({ value: n, label: n === 1 ? "1 (single)" : String(n) }))}
            />
          </Row>

          {selected && selected.regions.length > 0 && (
            <div className="flex flex-col gap-2">
              <div className="flex flex-col text-sm">
                <span className="font-medium">Regions</span>
                <span className="text-xs text-muted-foreground">
                  {s.regions.length === 0
                    ? "Any region with capacity. Pick regions to restrict; they are tried in the order picked."
                    : `Tried in order: ${s.regions.join(" → ")}`}
                </span>
              </div>
              <div className="flex max-h-56 flex-col overflow-y-auto rounded-lg border">
                {selected.regions.map((r) => {
                  const order = s.regions.indexOf(r.region);
                  return (
                    <button
                      key={r.region}
                      type="button"
                      onClick={() => toggleRegion(r.region)}
                      className={cn(
                        "flex items-center justify-between gap-3 border-b px-3 py-1.5 text-left text-sm last:border-0",
                        order >= 0 ? "bg-accent" : "hover:bg-accent/50",
                      )}
                    >
                      <span>
                        {order >= 0 && <span className="mr-2 text-xs text-primary">#{order + 1}</span>}
                        {r.region}
                        <span className="ml-2 text-xs text-muted-foreground">{r.instance_type}</span>
                      </span>
                      <span className="tabular-nums text-muted-foreground">
                        {money(r.price)} · spot {money(r.spot_price)}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          <Row label="Disk">
            <Input
              type="number"
              min={50}
              max={4000}
              className="w-24"
              value={s.diskSize}
              onChange={(e) => s.set({ diskSize: Math.min(4000, Math.max(50, Number(e.target.value) || 200)) })}
            />
            <span className="text-muted-foreground">GB</span>
          </Row>
        </>
      )}

      <Row label="Max runtime" hint="The job stops and saves a checkpoint at this limit.">
        <Input
          type="number"
          min={1}
          max={72}
          className="w-20"
          value={s.maxHours}
          onChange={(e) => s.set({ maxHours: Math.min(72, Math.max(1, Number(e.target.value) || 12)) })}
        />
        <span className="text-muted-foreground">hours</span>
      </Row>

      <Row label="Name">
        <Input className="w-56" value={s.name} placeholder="optional" onChange={(e) => s.set({ name: e.target.value })} />
      </Row>

      <Row label="When">
        <Select value={s.when} onValueChange={(v) => s.set({ when: v as When })}>
          <SelectTrigger className="w-44">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="now">Now</SelectItem>
            <SelectItem value="once">Once at…</SelectItem>
            <SelectItem value="hourly">Every N hours</SelectItem>
            <SelectItem value="daily">Daily</SelectItem>
            <SelectItem value="weekly">Weekly</SelectItem>
          </SelectContent>
        </Select>
      </Row>
      {s.when === "once" && (
        <Row label="Start at">
          <Input type="datetime-local" value={s.at} onChange={(e) => s.set({ at: e.target.value })} />
        </Row>
      )}
      {s.when === "hourly" && (
        <Row label="Every">
          <Input
            type="number"
            min={1}
            max={24}
            className="w-20"
            value={s.hours}
            onChange={(e) => s.set({ hours: Math.min(24, Math.max(1, Number(e.target.value) || 1)) })}
          />
          <span className="text-muted-foreground">hours</span>
        </Row>
      )}
      {(s.when === "daily" || s.when === "weekly") && (
        <Row label="Time">
          <Input type="time" value={s.time} onChange={(e) => s.set({ time: e.target.value })} />
        </Row>
      )}
      {s.when === "weekly" && (
        <Row label="Days">
          {WEEKDAYS.map((d, i) => (
            <Button
              key={d}
              type="button"
              size="sm"
              variant={s.days.includes(i + 1) ? "default" : "outline"}
              onClick={() =>
                s.set({ days: (s.days.includes(i + 1) ? s.days.filter((x) => x !== i + 1) : [...s.days, i + 1]).sort() })
              }
            >
              {d}
            </Button>
          ))}
        </Row>
      )}
    </div>
  );
}
