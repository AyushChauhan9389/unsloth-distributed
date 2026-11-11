// SPDX-License-Identifier: AGPL-3.0-only
// Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

import { createRoute, lazyRouteComponent } from "@tanstack/react-router";
import { requireAuth } from "../auth-guards";
import { Route as rootRoute } from "./__root";

const CloudJobsPage = lazyRouteComponent(
  () => import("@/features/cloud-jobs/cloud-jobs-page"),
  "CloudJobsPage",
);

export const Route = createRoute({
  getParentRoute: () => rootRoute,
  path: "/cloud-jobs",
  staticData: { titleKey: "shell.navigation.cloudJobs" },
  // ?job=<id> selects that job.
  validateSearch: (search: Record<string, unknown>): { job?: string } =>
    typeof search.job === "string" && search.job !== "" ? { job: search.job } : {},
  beforeLoad: () => requireAuth(),
  component: CloudJobsPage,
});
