// The ready-made `claude mcp add … --header "Authorization: Bearer mcp_…"` puts the token into the
// shell history; this variant reads it without echo into a variable first (security audit).
export function historySafeCommand(url: string): string {
  return `read -rs GANTT_MCP_TOKEN && claude mcp add --transport http planner ${url} --header "Authorization: Bearer $GANTT_MCP_TOKEN"`;
}
