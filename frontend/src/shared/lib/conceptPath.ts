// M-11 (260908 UI audit): `concept_path` (e.g. "/context/billing-overview.md")
// is a backend identifier shared with MCP resources and OKF export, where the
// ".md" suffix is real. No such file exists in the web UI, so strip it only
// for display here.
export function displayConceptPath(conceptPath: string): string {
  return conceptPath.replace(/\.md$/, "");
}
