import { apiRequest, type ApiWarning } from "./client";
import type { components } from "./types.gen";
import type { Job, JobStatus } from "./operations";

export type { Job, JobStatus };

// Both server-admin surfaces below scan every workspace's Job
// table and sum/merge the results, so both response shapes carry
// unreachable_workspace_ids -- non-empty means some workspace's shard could
// not be reached, so the count/list is a partial one, not a short-but-
// complete one. Distinct from the workspace-scoped JobListData (operations.ts),
// which never fans out and so never has this field.
export type AdminJobQueueSummary =
  components["schemas"]["JobQueueSummaryData"];
export type AdminJobListData = components["schemas"]["AdminJobListData"];

export type AdminJobFilters = {
  workspace_id?: string;
  status?: JobStatus;
  job_type?: string;
  problems_only?: boolean;
  limit?: number;
  cursor?: string | null;
};

// Both admin endpoints below surface fan-out incompleteness as an envelope
// warning (code "job_fanout_incomplete") in addition to the structured
// unreachable_workspace_ids list -- the warning is the generic "something is
// off" signal, the id list is what the UI renders a specific count from.
export type AdminJobEnvelopeWarnings = ApiWarning[];

function encodeQuery(
  params: Record<string, string | number | boolean | null | undefined>,
) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      query.set(key, String(value));
    }
  }
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

export async function fetchAdminJobQueueSummary() {
  const response = await apiRequest<{
    envelope: { warnings: AdminJobEnvelopeWarnings };
    data: AdminJobQueueSummary;
  }>("/api/v1/admin/jobs/summary");
  return { ...response.data, warnings: response.envelope.warnings };
}

export async function listAdminJobs(filters: AdminJobFilters = {}) {
  const query = encodeQuery(filters);
  const response = await apiRequest<{
    envelope: { warnings: AdminJobEnvelopeWarnings };
    data: AdminJobListData;
  }>(`/api/v1/admin/jobs${query}`);
  return { ...response.data, warnings: response.envelope.warnings };
}

export async function retryAdminJob(jobId: string, workspaceId: string) {
  // workspace_id is required (Epic-2 Phase 6c): once Job is workspace-plane
  // data the endpoint has to be told which shard to mutate through, and the
  // admin listing this button is rendered from already puts workspace_id on
  // every row (Job.workspace_id), so the caller always has it in hand.
  const response = await apiRequest<{ data: Job }>(
    `/api/v1/admin/jobs/${encodeURIComponent(jobId)}/retry${encodeQuery({ workspace_id: workspaceId })}`,
    { method: "POST" },
  );
  return response.data;
}
