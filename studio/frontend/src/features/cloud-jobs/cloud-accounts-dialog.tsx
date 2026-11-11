// SPDX-License-Identifier: AGPL-3.0-only
// Connect AWS (access keys, optional IAM role) and Google Cloud (service-account key) for cloud jobs.
// Secrets are only ever sent to the server; the server never sends them back.

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/lib/toast";
import { type ReactElement, useCallback, useEffect, useState } from "react";
import { type CloudAccounts, connectAws, connectGcp, disconnectCloud, getCloudAccounts } from "./api";

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

function Connected({ title, lines, onDisconnect }: { title: string; lines: string[]; onDisconnect: () => void }) {
  return (
    <div className="flex items-start justify-between gap-3 rounded-lg border p-3 text-sm">
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className="font-medium">
          {title} <span className="ml-1 text-xs text-primary">connected</span>
        </span>
        {lines.map((l) => (
          <span key={l} className="break-all text-xs text-muted-foreground">
            {l}
          </span>
        ))}
      </div>
      <Button size="sm" variant="outline" onClick={onDisconnect}>
        Disconnect
      </Button>
    </div>
  );
}

export function CloudAccountsDialog({
  open,
  onOpenChange,
  onChanged,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onChanged?: () => void;
}): ReactElement {
  const [accounts, setAccounts] = useState<CloudAccounts | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"aws" | "gcp" | null>(null);
  const [keyId, setKeyId] = useState("");
  const [secret, setSecret] = useState("");
  const [region, setRegion] = useState("us-east-1");
  const [roleArn, setRoleArn] = useState("");
  const [gcpKey, setGcpKey] = useState("");
  const [gcpFile, setGcpFile] = useState("");

  const load = useCallback(() => {
    setLoadError(null);
    getCloudAccounts().then(setAccounts).catch((e) => setLoadError(errText(e)));
  }, []);
  useEffect(() => {
    if (open) load();
  }, [open, load]);

  const done = (next: CloudAccounts | null, msg: string) => {
    if (next) setAccounts(next);
    else load();
    onChanged?.();
    toast.success(msg);
  };

  const saveAws = async () => {
    setBusy("aws");
    try {
      await connectAws({ access_key_id: keyId, secret_access_key: secret, region, role_arn: roleArn || undefined });
      setSecret("");
      setKeyId("");
      done(null, "AWS connected");
    } catch (e) {
      toast.error(errText(e));
    } finally {
      setBusy(null);
    }
  };

  const saveGcp = async () => {
    setBusy("gcp");
    try {
      await connectGcp(gcpKey);
      setGcpKey("");
      setGcpFile("");
      done(null, "Google Cloud connected");
    } catch (e) {
      toast.error(errText(e));
    } finally {
      setBusy(null);
    }
  };

  const disconnect = async (cloud: "aws" | "gcp") => {
    try {
      done(await disconnectCloud(cloud), `${cloud === "aws" ? "AWS" : "Google Cloud"} disconnected`);
    } catch (e) {
      toast.error(errText(e));
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Cloud accounts</DialogTitle>
          <DialogDescription>
            Credentials are checked with the cloud before saving, stored only on this server, and never shown again.
            Use a dedicated IAM user or service account with only the permissions SkyPilot needs.
          </DialogDescription>
        </DialogHeader>
        {loadError && <p className="text-sm text-destructive">{loadError}</p>}
        {!accounts && !loadError && <Spinner className="size-4" />}
        {accounts && (
          <div className="flex flex-col gap-6">
            <section className="flex flex-col gap-3">
              <h3 className="text-sm font-semibold">AWS</h3>
              {accounts.aws ? (
                <Connected
                  title="AWS"
                  lines={[
                    `Account ${accounts.aws.account} · ${accounts.aws.region}`,
                    accounts.aws.arn,
                    `Key ${accounts.aws.access_key_id}${accounts.aws.role_arn ? ` · assumes ${accounts.aws.role_arn}` : ""}`,
                  ]}
                  onDisconnect={() => void disconnect("aws")}
                />
              ) : (
                <form
                  className="flex flex-col gap-2"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void saveAws();
                  }}
                >
                  <Input placeholder="Access key ID" autoComplete="off" value={keyId} onChange={(e) => setKeyId(e.target.value)} />
                  <Input
                    type="password"
                    placeholder="Secret access key"
                    autoComplete="new-password"
                    value={secret}
                    onChange={(e) => setSecret(e.target.value)}
                  />
                  <Input placeholder="Default region, e.g. us-east-1" value={region} onChange={(e) => setRegion(e.target.value)} />
                  <Input
                    placeholder="IAM role ARN to assume (optional), arn:aws:iam::123456789012:role/..."
                    value={roleArn}
                    onChange={(e) => setRoleArn(e.target.value)}
                  />
                  <Button type="submit" disabled={busy !== null || !keyId || !secret || !region}>
                    {busy === "aws" && <Spinner className="size-4" />} Connect AWS
                  </Button>
                </form>
              )}
            </section>

            <section className="flex flex-col gap-3">
              <h3 className="text-sm font-semibold">Google Cloud</h3>
              {accounts.gcp ? (
                <Connected
                  title="Google Cloud"
                  lines={[`Project ${accounts.gcp.project_id}`, accounts.gcp.client_email]}
                  onDisconnect={() => void disconnect("gcp")}
                />
              ) : (
                <div className="flex flex-col gap-2">
                  <p className="text-xs text-muted-foreground">
                    Upload a service-account JSON key (IAM → Service accounts → Keys → Add key → JSON). The project is read
                    from the key.
                  </p>
                  <Input
                    type="file"
                    accept="application/json,.json"
                    onChange={async (e) => {
                      const f = e.target.files?.[0];
                      if (!f) return;
                      setGcpFile(f.name);
                      setGcpKey(await f.text());
                    }}
                  />
                  {gcpFile && <span className="text-xs text-muted-foreground">{gcpFile} ready</span>}
                  <Button disabled={busy !== null || !gcpKey} onClick={() => void saveGcp()}>
                    {busy === "gcp" && <Spinner className="size-4" />} Connect Google Cloud
                  </Button>
                </div>
              )}
            </section>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
