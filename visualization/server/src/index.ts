import { resolve, delimiter } from 'node:path';
import { projectHandlers } from './providers/project-scope';
import { allowLocalRequest } from './local-access';
import { Hono } from 'hono';
import { cors } from 'hono/cors';
import { AgentStateManager } from './state/agent-state-manager';
import type { SessionMeta } from './state/agent-state-manager';
import { SessionRegistry } from './session-registry';
import { WebSocketServer } from './ws/websocket-server';
import type { WsClient } from './ws/websocket-server';
import { MapStorage } from './map/storage';
import { registerMapRoutes } from './map/routes';
import { ClaudeProvider } from './providers/claude-provider';
import { CodexProvider } from './providers/codex-provider';
import type { ProviderHandlers, SessionStartPayload, SessionEventsPayload } from './providers/types';
import type { ParsedEvent } from './parsers/session-parser';
import type { AgentSource, AgentState, SkillInventory } from './types';
import { claudeSkillRoots, codexSkillRoots, inventoryChanged, scanSkillInventory } from './skills/skill-inventory';

const PORT = Number(process.env.PORT) || 4444;
const CLIENT_PORT = Number(process.env.CLIENT_PORT) || 4445;
const PROJECT_ROOT = resolve(import.meta.dir, '../../..');
const IDLE_THRESHOLD_MS = 5 * 60_000;       // 5 minutes without events → idle
const COMPLETED_THRESHOLD_MS = 30 * 60_000; // 30 minutes idle → completed
const STALE_THRESHOLD_MS = 2 * 60 * 60_000; // 2 hours completed → remove (keep min 5)
const SESSION_MAX_AGE_MS = 3 * 60 * 60_000; // skip JSONL files older than 3h on startup
const MIN_VISIBLE_AGENTS = 5;               // always keep at least 5 agents visible
// Subagents have no pidfile, so we lean on the `busy` flag (set by JSONL tool
// events, cleared by turn-end) to decide between "still working" and "finished".
// Busy subagents stay active up to the crash timeout; non-busy ones get a
// tight idle/completed sweep so the dashboard doesn't accumulate ghosts.
const SUBAGENT_IDLE_THRESHOLD_MS = 120_000;                // 2 min silent, post turn-end → idle
const SUBAGENT_COMPLETED_THRESHOLD_MS = 5 * 60_000;        // 5 min idle → completed
const SUBAGENT_BUSY_COMPLETED_THRESHOLD_MS = 15 * 60_000;  // 15 min busy silent → presumed crashed

const app = new Hono();
const sessionRegistry = new SessionRegistry({ configDirs: [] });
const stateManager = new AgentStateManager({
  idleThresholdMs: IDLE_THRESHOLD_MS,
  completedThresholdMs: COMPLETED_THRESHOLD_MS,
  subagentIdleThresholdMs: SUBAGENT_IDLE_THRESHOLD_MS,
  subagentCompletedThresholdMs: SUBAGENT_COMPLETED_THRESHOLD_MS,
  subagentBusyCompletedThresholdMs: SUBAGENT_BUSY_COMPLETED_THRESHOLD_MS,
  livenessOracle: sessionRegistry,
  // Optional escape hatch for installs whose settings.json doesn't reveal the window.
  claudeContextWindowOverride: process.env.AGENT_QUEST_CLAUDE_CONTEXT_WINDOW,
});
const wsServer = new WebSocketServer();
const mapStorage = new MapStorage();

// The observer is local-only, including WebSocket upgrades.
app.use('*', cors({
  origin: (origin) => ['127.0.0.1', 'localhost', '[::1]'].some((host) => origin === `http://${host}:${CLIENT_PORT}`) ? origin : undefined,
}));

// --- HTTP endpoints ---
app.get('/api/health', (c) => c.json({ status: 'ok', agents: stateManager.getAll().length, clients: wsServer.clientCount }));

app.get('/api/agents', (c) => c.json(stateManager.getAll()));

registerMapRoutes(app, mapStorage);

// --- Provider handlers: shared logic that every SessionProvider feeds into ---
function broadcastAgentEventSideEffects(event: ParsedEvent, agent: AgentState): void {
  // Broadcast activity log for tool calls. Skill rows show the skill name —
  // the tool has no file/command to show otherwise.
  for (const tc of event.toolCalls) {
    const skill = tc.name === 'Skill' && typeof tc.input['skill'] === 'string' ? tc.input['skill'] : undefined;
    const detail = skill ?? event.file ?? event.command ?? '';
    wsServer.broadcastActivityLog(event.sessionId, tc.name, detail, tc.timestamp);
  }

  // Codex loads skills by reading SKILL.md through the shell — there is no
  // Skill tool call to show, so synthesize the same feed row Claude gets.
  if (event.skillInvocations !== undefined && !event.toolCalls.some((tc) => tc.name === 'Skill')) {
    for (const name of event.skillInvocations) {
      wsServer.broadcastActivityLog(event.sessionId, 'Skill', name, event.timestamp);
    }
  }

  // Context compaction: the conversation was summarized to make room.
  if (event.isCompaction === true) {
    const n = agent.context?.compactions ?? 1;
    wsServer.broadcastActivityLog(event.sessionId, 'Compact', `context compacted (#${n})`, event.timestamp);
  }

  // User prompts: feed shows the message that kicked off the turn.
  // Skip resume hints (historical last-prompt dumps) and subagents (their
  // "prompt" is the parent's tool_use input, already visible as a Task row).
  if (
    event.kind === 'task' &&
    event.currentTask !== undefined &&
    event.isResumeHint !== true &&
    !agent.isSubagent
  ) {
    const detail = event.currentTask.length > 500
      ? `${event.currentTask.slice(0, 500)}…`
      : event.currentTask;
    wsServer.broadcastActivityLog(event.sessionId, 'Prompt', detail, event.timestamp);
  }

  // Claude's text reply at turn-end. The full message lives on AgentState
  // (DetailPanel lets the user expand it), but the activity feed row stays
  // compact — cap it here so scrolling the feed doesn't render walls of text.
  if (event.isTurnEnd === true && event.lastMessage !== undefined) {
    const feedDetail = event.lastMessage.length > 300
      ? `${event.lastMessage.slice(0, 300)}…`
      : event.lastMessage;
    wsServer.broadcastActivityLog(event.sessionId, 'Reply', feedDetail, event.timestamp);
  }
}

function sessionMetaOf(payload: SessionStartPayload | SessionEventsPayload): SessionMeta {
  return {
    configDir: payload.configDir,
    source: payload.source,
    nameOverride: payload.nameOverride,
    isSubagent: payload.isSubagent,
    parentId: payload.parentId,
    subagentType: payload.subagentType,
    isWorkflow: payload.isWorkflow,
    spawnToolUseId: payload.spawnToolUseId,
    configuredModel: payload.configuredModel,
  };
}

const providerHandlers: ProviderHandlers = projectHandlers(PROJECT_ROOT, {
  onSessionStart: async (payload: SessionStartPayload) => {
    console.log(`[Server] new session: ${payload.sessionId} (${payload.source}, ${payload.configDir})`);
    const meta = sessionMetaOf(payload);
    for (const event of payload.events) {
      // Subagent JSONL lines carry the PARENT's sessionId — rekey on the
      // filename-derived id so each subagent becomes its own hero instead
      // of being folded into the parent agent.
      event.sessionId = payload.sessionId;
      const result = stateManager.processEvent(event, meta);
      if (result !== null && result.isNew) {
        wsServer.broadcastNewAgent(result.agent);
        // A parent discovered after its subagents recolors them.
        for (const other of result.alsoChanged ?? []) wsServer.broadcastAgentUpdate(other);
      }
    }
    // Broadcast final state after processing all events
    const agent = stateManager.getAgent(payload.sessionId);
    if (agent !== undefined) wsServer.broadcastAgentUpdate(agent);
  },

  onSessionEvents: (payload: SessionEventsPayload) => {
    const meta = sessionMetaOf(payload);
    for (const event of payload.events) {
      // See note in onSessionStart: rekey to filename-derived id for subagents.
      event.sessionId = payload.sessionId;
      const result = stateManager.processEvent(event, meta);
      if (result === null) continue; // resume-hint dump or telemetry with no visible change

      if (result.isNew) {
        wsServer.broadcastNewAgent(result.agent);
      } else {
        wsServer.broadcastAgentUpdate(result.agent);
      }
      for (const other of result.alsoChanged ?? []) {
        wsServer.broadcastAgentUpdate(other);
      }

      broadcastAgentEventSideEffects(event, result.agent);
    }
  },
});

const claudeProvider = new ClaudeProvider({ maxAgeMs: SESSION_MAX_AGE_MS, claudeDirs: process.env.PATKONG_CLAUDE_DIRS?.split(delimiter) });
const codexProvider = new CodexProvider({ maxAgeMs: SESSION_MAX_AGE_MS, codexRoot: process.env.PATKONG_CODEX_ROOT });

function allConfigDirs(): string[] {
  return [...claudeProvider.getConfigDirs(), ...codexProvider.getConfigDirs()];
}

// --- Skill inventory: what every installed skill would cost if always loaded ---
// Shown next to the per-session skill loads so the saving of on-demand loading
// is visible. Cheap (one stat per SKILL.md), refreshed every 10 minutes.
const SKILL_INVENTORY_REFRESH_MS = 10 * 60_000;
let skillInventory: Partial<Record<AgentSource, SkillInventory>> = {};

async function scanInstalledSkills(): Promise<Partial<Record<AgentSource, SkillInventory>>> {
  const next: Partial<Record<AgentSource, SkillInventory>> = {};
  const claudeRoots: string[] = [];
  for (const dir of claudeProvider.getConfigDirs()) claudeRoots.push(...await claudeSkillRoots(dir));
  if (claudeRoots.length > 0) next.claude = await scanSkillInventory(claudeRoots);
  const codexRoot = codexProvider.getConfigDirs()[0];
  if (codexRoot !== undefined) next.codex = await scanSkillInventory(codexSkillRoots(codexRoot));
  return next;
}

/** Rescan; when the installed set changed, push it to every open dashboard. */
async function refreshSkillInventory(): Promise<void> {
  const next = await scanInstalledSkills();
  if (!inventoryChanged(skillInventory, next)) return;
  skillInventory = next;
  wsServer.broadcastSkillInventory(skillInventory);
}

// --- Lifecycle: active → idle (5m) → completed (30m) → removed (2h, keep min 5) ---
setInterval(() => {
  // Phase 1: active → idle (5 min without events)
  const idled = stateManager.checkIdleAgents(IDLE_THRESHOLD_MS);
  for (const sessionId of idled) {
    const agent = stateManager.getAgent(sessionId);
    if (agent !== undefined) {
      wsServer.broadcastAgentUpdate(agent);
    }
  }

  // Phase 2: idle → completed (30 min without events)
  const completed = stateManager.checkCompletedAgents(COMPLETED_THRESHOLD_MS);
  for (const sessionId of completed) {
    const agent = stateManager.getAgent(sessionId);
    if (agent !== undefined) {
      wsServer.broadcastAgentUpdate(agent);
    }
  }

  // Phase 3: remove completed agents older than 2h (always keep at least 5)
  const removed = stateManager.cleanupStaleAgents(STALE_THRESHOLD_MS, MIN_VISIBLE_AGENTS);
  for (const sessionId of removed) {
    wsServer.broadcastAgentComplete(sessionId);
  }

  if (removed.length > 0) {
    console.log(`[Server] cleaned up ${removed.length} stale agent(s)`);
  }
}, 30_000);

// Start both providers before the HTTP server so the very first client
// connection's snapshot already carries the discovered configDirs (instead
// of racing the async auto-discovery and incorrectly reporting "no agent
// CLI install").
await claudeProvider.start(providerHandlers);
await codexProvider.start(providerHandlers);

await refreshSkillInventory().catch((err) => console.error('[Server] skill inventory scan failed:', err));
setInterval(() => {
  refreshSkillInventory().catch((err) => console.error('[Server] skill inventory scan failed:', err));
}, SKILL_INVENTORY_REFRESH_MS);

// If neither provider found anything on disk, emit a single aggregated
// warning so the user sees one clear diagnostic line instead of per-provider
// chatter. The client banner shows the equivalent message to the user.
if (allConfigDirs().length === 0) {
  console.warn('[Server] WARNING: no Claude Code or Codex install detected. Start a session with either to see heroes here.');
}

// Seed the liveness registry with the same config dirs the watcher just
// auto-discovered, then prime it synchronously so the first WS snapshot can
// already filter out phantom sessions. Registry stays Claude-only by design —
// the pidfile oracle is a Claude-specific signal; Codex liveness is inferred
// purely from rollout-file activity.
sessionRegistry.setConfigDirs(claudeProvider.getConfigDirs());
await sessionRegistry.start(10_000);
console.log(`[SessionRegistry] live session ids: ${sessionRegistry.snapshot().length}`);

// Every time the registry refreshes, re-derive every agent's status so that
// sessions that just died get demoted to `completed`, and resumed pids come
// back into view without waiting for the next JSONL event.
setInterval(() => {
  const changed = stateManager.refreshAll();
  for (const sessionId of changed) {
    const agent = stateManager.getAgent(sessionId);
    if (agent !== undefined) wsServer.broadcastAgentUpdate(agent);
  }
}, 10_000);

// --- Start ---
const server = Bun.serve<{ id: string }>({
  hostname: '127.0.0.1',
  port: PORT,
  fetch(req, server) {
    if (!allowLocalRequest(req, PORT, CLIENT_PORT)) return new Response('Forbidden', { status: 403 });
    // WebSocket upgrade
    if (new URL(req.url).pathname === '/ws') {
      const id = crypto.randomUUID();
      const upgraded = server.upgrade(req, { data: { id } });
      if (upgraded) return undefined;
      return new Response('WebSocket upgrade failed', { status: 400 });
    }

    // Hono handles HTTP
    return app.fetch(req);
  },
  websocket: {
    open(ws: WsClient) {
      wsServer.handleOpen(ws);
      wsServer.sendSnapshot(ws, stateManager.getAll(), allConfigDirs(), skillInventory);
    },
    close(ws: WsClient) {
      wsServer.handleClose(ws);
    },
    message() {
      // Client-to-server messages not needed in Phase 1
    },
  },
});

console.log(`[Server] Patkong observer running on http://127.0.0.1:${PORT}`);
console.log(`[Server] WebSocket endpoint: ws://localhost:${PORT}/ws`);
console.log(`[Server] Health check: http://localhost:${PORT}/api/health`);


// Stop polling and close sockets together on Ctrl+C.
function shutdown(): void {
  claudeProvider.stop();
  codexProvider.stop();
  server.stop(true);
  process.exit(0);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
