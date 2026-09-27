import { expect, test, type APIRequestContext } from "@playwright/test";

// A mass deletion requested by an external MCP client can't confirm itself (text in a plan or a
// file could have told the client to): it waits for the user's approval in the app.
async function mcpCall(request: APIRequestContext, token: string, args: Record<string, unknown>) {
  const r = await request.post("/mcp/", {
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", Accept: "application/json, text/event-stream" },
    data: { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "apply_operations", arguments: args } },
  });
  expect(r.status()).toBe(200);
  const body = await r.json();
  return { isError: Boolean(body.result?.isError), text: String(body.result?.content?.[0]?.text ?? "") };
}

test("an MCP mass deletion runs only after approval in the browser", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  const token = (await (await page.request.post("/api/mcp-token")).json()).token as string;
  const plan = async () => (await page.request.get("/api/plan")).json();
  const { version } = await plan();
  const args = { operations: [1, 2, 3, 4, 5, 6].map((id) => ({ op: "delete_task", id })), expected_version: version };

  const first = await mcpCall(page.request, token, args);
  expect(first.isError && first.text.startsWith("confirmation_required")).toBe(true);
  const banner = page.getByRole("alert").filter({ hasText: "Внешний MCP-клиент просит удалить задачи (6)" });
  await expect(banner).toBeVisible();

  // The client can't approve itself by just retrying with confirmed=true.
  const selfConfirmed = await mcpCall(page.request, token, { ...args, confirmed: true });
  expect(selfConfirmed.text).toMatch(/^confirmation_required/);
  expect((await plan()).plan.tasks).toHaveLength(25);

  await banner.getByRole("button", { name: "Разрешить удаление" }).click();
  await expect(banner).toContainText("Разрешено");

  const approved = await mcpCall(page.request, token, { ...args, confirmed: true });
  expect(approved.isError, approved.text).toBe(false);
  await expect.poll(async () => (await plan()).plan.tasks.length).toBe(19);
  await expect(page.getByRole("alert").filter({ hasText: "Внешний MCP-клиент" })).toHaveCount(0);
});
