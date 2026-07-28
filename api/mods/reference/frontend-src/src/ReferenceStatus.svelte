<!--
  The reference mod's ONE status view (cavekit-mods-frontend-client.md R7).

  A proof of the mechanism, NOT a feature: it shows the reference mod's current
  async-handle state ({task_id, status}) and offers a single button that
  triggers the mod's `submit` tool. There are deliberately no forms, no history,
  and no settings surfaces (R7 AC7) --- exactly one view.

  Contract this component fulfils:
    * Compiled as a Svelte 5 custom element <mod-reference> that self-registers
      via customElements.define() from its own bundle (R7 AC1 / R3-5). The tag
      string is the value naming.custom_element_tag_for("reference") derives
      ("mod-" + id, underscores folded to hyphens) === "mod-reference"; the
      reference mod's mod.yaml leaves `tag` undeclared, so this derived value is
      what T-A05's manifest reports and what self.chat's host creates.
    * Receives auth/context by property assignment, exposed here via $props()
      under the exact names the host sets before insertion (R3):
      `el.authToken`, `el.apiBase`, `el.currentUser`.
    * Shadow DOM stays ON (Svelte's custom-element default); the <style> block
      below is scoped and injected into the shadow root by Svelte --- host
      Tailwind does not penetrate, which is the intended isolation.

  NOTE ON MY OWN VALIDATION SCOPE (T-C08): this task owns AC1 (compiles +
  self-registers) and AC7 (exactly one view). The LIVE proof --- that the fetch,
  the socket subscription, and the button actually drive the running reference
  mod --- is T-C09's job, run against the real app. The logic below is written
  to be genuinely correct for that live run (not stubbed), but its runtime
  behaviour was not executed here.
-->
<svelte:options customElement="mod-reference" />

<script>
	import { onMount, onDestroy } from 'svelte';
	import { io } from 'socket.io-client';

	// Context handed in by the host via property assignment BEFORE this element
	// is inserted into the DOM (R3). These exact names mirror self.chat's
	// mountModElement(): el.authToken, el.apiBase, el.currentUser.
	let { authToken = '', apiBase = '', currentUser = null } = $props();

	// The reference mod's async-handle state, {task_id, status}, plus the count
	// the mod's STATE.snapshot() carries.
	let latest = $state(null);
	let count = $state(0);

	let connected = $state(false);
	let submitting = $state(false);
	let errorText = $state('');

	let socket;

	// apiBase is self.chat's WEBUI_API_BASE_URL, shaped `${origin}/api/v1`. The
	// reference mod's own route (`GET /reference/state`, T-005) and its
	// Socket.IO namespace (`/reference`, T-004) live at the API ORIGIN, NOT under
	// /api/v1 --- the mod's router mounts at its manifest `api.prefix` (/reference)
	// on the app root. So derive the origin by stripping the /api/v1 suffix.
	function apiOrigin() {
		const base = (apiBase || '').replace(/\/api\/v1\/?$/, '');
		if (base) return base.replace(/\/$/, '');
		return typeof location !== 'undefined' ? location.origin : '';
	}

	function applySnapshot(snap) {
		if (!snap) return;
		latest = snap.latest ?? null;
		count = typeof snap.count === 'number' ? snap.count : 0;
	}

	// R1's read path: fetch the current snapshot from the mod's real route with
	// the injected token as a Bearer header. A client that never subscribed can
	// still observe the tool's latest recorded change this way.
	async function fetchState() {
		try {
			const res = await fetch(`${apiOrigin()}/reference/state`, {
				headers: { Authorization: `Bearer ${authToken}` }
			});
			if (!res.ok) {
				errorText = `state request failed (${res.status})`;
				return;
			}
			applySnapshot(await res.json());
			errorText = '';
		} catch (e) {
			errorText = `state request error: ${e instanceof Error ? e.message : String(e)}`;
		}
	}

	// Live updates (R7 AC4): subscribe to the mod's existing /reference namespace
	// on core's Socket.IO server. The namespace's connect gate
	// (install_namespace_auth) authenticates from auth.token, so the token rides
	// the handshake --- never a URL query. On connect we emit "sync" (the mod's
	// on-subscribe pull, T-004) and listen for the one event the mod pushes,
	// "reference:state", carrying the same STATE.snapshot() shape.
	function connectSocket() {
		socket = io(`${apiOrigin()}/reference`, {
			path: '/ws/socket.io',
			auth: { token: authToken },
			transports: ['websocket', 'polling']
		});
		socket.on('connect', () => {
			connected = true;
			socket.emit('sync');
		});
		socket.on('disconnect', () => {
			connected = false;
		});
		socket.on('reference:state', (snap) => applySnapshot(snap));
		socket.on('connect_error', (e) => {
			errorText = `socket error: ${e instanceof Error ? e.message : String(e)}`;
		});
	}

	// R7 AC5: the button triggers the reference mod's `submit` tool.
	//
	// HONEST CONSTRAINT (surfaced for T-C09): Phase 1 exposes NO direct external
	// HTTP surface for `submit`. It is a model-callable tool dispatched only
	// through core's chat/tool-calling path (impl-mods-reference-implementation.md
	// T-003/T-010: invoked via assemble_for_user + the _scope_guard callable, not
	// a REST route). The only external route the mod mounts is GET /reference/state.
	//
	// This POST targets the conventional route (`POST /reference/submit`) that a
	// follow-up backend surface would expose so the view can trigger the tool
	// WITHOUT round-tripping an LLM. Until that route exists it fails visibly (a
	// 404 into errorText) rather than faking success. Either that route must be
	// added to the reference mod, or T-C09 must drive `submit` through the chat
	// tool-call path to prove this AC. See the T-C08 report finding.
	async function triggerSubmit() {
		submitting = true;
		errorText = '';
		try {
			const res = await fetch(`${apiOrigin()}/reference/submit`, {
				method: 'POST',
				headers: {
					Authorization: `Bearer ${authToken}`,
					'Content-Type': 'application/json'
				},
				body: '{}'
			});
			if (!res.ok) {
				errorText = `submit failed (${res.status}) --- no direct tool route yet; see T-C08 finding`;
			} else {
				// The mod's tool write does not auto-push over the namespace
				// (T-010), so pull the fresh snapshot to reflect the change.
				await fetchState();
			}
		} catch (e) {
			errorText = `submit error: ${e instanceof Error ? e.message : String(e)}`;
		} finally {
			submitting = false;
		}
	}

	onMount(() => {
		fetchState();
		connectSocket();
	});

	// Teardown (R6): the mod author is responsible for cleaning up non-Svelte
	// side effects --- here the raw Socket.IO connection --- in onDestroy.
	onDestroy(() => {
		try {
			socket?.disconnect();
		} catch {
			// disconnecting an already-closed socket is inert
		}
	});

	const userLabel = $derived(
		currentUser?.name || currentUser?.email || currentUser?.id || 'unknown'
	);
</script>

<section class="reference-status" data-testid="reference-status">
	<header>
		<h2>Reference Mod</h2>
		<span class="conn" class:online={connected} title={connected ? 'subscribed' : 'not subscribed'}>
			{connected ? 'live' : 'offline'}
		</span>
	</header>

	<dl class="state">
		<dt>task_id</dt>
		<dd>{latest?.task_id ?? '—'}</dd>
		<dt>status</dt>
		<dd>{latest?.status ?? '—'}</dd>
		<dt>count</dt>
		<dd>{count}</dd>
	</dl>

	<button onclick={triggerSubmit} disabled={submitting}>
		{submitting ? 'Submitting…' : 'Submit work'}
	</button>

	{#if errorText}
		<p class="error" role="alert">{errorText}</p>
	{/if}

	<footer>as {userLabel}</footer>
</section>

<style>
	.reference-status {
		font-family: system-ui, sans-serif;
		color: #e5e7eb;
		background: #111827;
		border: 1px solid #374151;
		border-radius: 8px;
		padding: 1rem 1.25rem;
		max-width: 22rem;
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}
	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}
	h2 {
		margin: 0;
		font-size: 1rem;
		font-weight: 600;
	}
	.conn {
		font-size: 0.75rem;
		padding: 0.1rem 0.5rem;
		border-radius: 999px;
		background: #374151;
		color: #9ca3af;
	}
	.conn.online {
		background: #064e3b;
		color: #34d399;
	}
	dl.state {
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 0.25rem 0.75rem;
		margin: 0;
		font-size: 0.875rem;
	}
	dt {
		color: #9ca3af;
	}
	dd {
		margin: 0;
		font-variant-numeric: tabular-nums;
		word-break: break-all;
	}
	button {
		appearance: none;
		border: 0;
		border-radius: 6px;
		padding: 0.5rem 0.75rem;
		background: #2563eb;
		color: #fff;
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
	}
	button:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.error {
		margin: 0;
		font-size: 0.8rem;
		color: #fca5a5;
	}
	footer {
		font-size: 0.75rem;
		color: #6b7280;
	}
</style>
