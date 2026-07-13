import assert from "node:assert/strict";
import { createServer } from "node:http";
import geothermPricebook from "../src/data/geotherm-pricebook.json";
import { startChatServer } from "./chat-server";

async function readBody(request: import("node:http").IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

async function main(): Promise<void> {
  const leads: unknown[] = [];
  const events: unknown[] = [];
  const apiKey = "odoo-contract-test-key";
  const mockOdoo = createServer(async (request, response) => {
    assert.equal(request.headers.authorization, `Bearer ${apiKey}`);
    if (request.method === "GET" && request.url === "/pricebook") {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ ...geothermPricebook, version: "odoo-contract-version" }));
      return;
    }
    if (request.method === "POST" && request.url === "/leads") {
      leads.push(await readBody(request));
      response.writeHead(201, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ ok: true }));
      return;
    }
    if (request.method === "POST" && request.url === "/events") {
      events.push(await readBody(request));
      response.writeHead(201, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ ok: true }));
      return;
    }
    response.writeHead(404).end();
  });
  await new Promise<void>((resolve) => mockOdoo.listen(0, "127.0.0.1", resolve));
  const mockAddress = mockOdoo.address();
  assert.ok(mockAddress && typeof mockAddress === "object");

  process.env.ODOO_API_KEY = apiKey;
  process.env.ODOO_PRICEBOOK_URL = `http://127.0.0.1:${mockAddress.port}/pricebook`;
  process.env.ODOO_LEAD_URL = `http://127.0.0.1:${mockAddress.port}/leads`;
  process.env.ODOO_ANALYTICS_URL = `http://127.0.0.1:${mockAddress.port}/events`;
  process.env.ARCIGY_LLM_ENABLED = "false";

  let chatServer: Awaited<ReturnType<typeof startChatServer>> | null = null;
  try {
    chatServer = await startChatServer({
      port: 0,
      host: "127.0.0.1",
      rateLimit: { enabled: false },
      siteSignature: { enabled: false },
    });
    const chatAddress = chatServer.address();
    assert.ok(chatAddress && typeof chatAddress === "object");
    const base = `http://127.0.0.1:${chatAddress.port}`;
    const anonymousId = `odoo_contract_${Date.now()}`;
    const send = (message: string) => fetch(`${base}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, siteId: "geotherm", anonymousId }),
      signal: AbortSignal.timeout(20000),
    }).then((response) => response.json());

    const health = await fetch(`${base}/health`, { signal: AbortSignal.timeout(5000) }).then((response) => response.json()) as { pricebook?: { version?: string; synced?: boolean } };
    assert.equal(health.pricebook?.version, "odoo-contract-version");
    assert.equal(health.pricebook?.synced, true);

    await send("Chcem tepelné čerpadlo pre rekonštrukciu domu 150 m2 s radiátormi.");
    await send("Chcem, aby mi zavolal obchodník. Telefón je 0905 123 456.");
    assert.ok(events.length >= 2, "Odoo must receive one analytics event per turn");
    assert.equal((events[0] as { event?: string }).event, "chat.turn");
    assert.ok(leads.length >= 1, "Odoo must receive captured lead");
    const lastLead = leads.at(-1) as { lead?: { contact?: { phone?: string }; transcript?: unknown[] } };
    assert.equal(lastLead.lead?.contact?.phone, "0905123456");
    assert.ok((lastLead.lead?.transcript?.length || 0) >= 2, "Lead payload must contain transcript");
    console.log("PASS Odoo pricebook, analytics and CRM webhook contract");
  } finally {
    if (chatServer) await new Promise<void>((resolve) => chatServer?.close(() => resolve()));
    await new Promise<void>((resolve) => mockOdoo.close(() => resolve()));
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
