/**
 * Talk view: conversation orb driven by the SSE activity stream.
 * Audio I/O runs entirely in Python; the orb doubles as the mic toggle.
 * Robot stays live, tapping the orb only mutes or unmutes the user's mic.
 */

import {
  API_PREFIX,
  applyPersonality,
  describeError,
  getMicState,
  listPersonalities,
  sendText,
  setMicMuted,
} from "../api.js";
import { BUILT_IN_DEFAULT_OPTION, ORB_STATES } from "../constants.js";
import { createOrb, mapActivityToState } from "../orb.js";
import { consumePendingApply } from "../pending-apply.js";
import { setPersonality } from "../personality-badge.js";
import { h, prettifyProfileName } from "../ui.js";

const SSE_ENDPOINT = `${API_PREFIX}/conversation_events`;

const CAPTION_BY_STATE = Object.freeze({
  [ORB_STATES.MUTED]: "Muted",
  [ORB_STATES.IDLE]: "Ready",
  [ORB_STATES.CONNECTING]: "Connecting to the backend...",
  [ORB_STATES.LISTENING]: "Listening",
  [ORB_STATES.THINKING]: "Thinking",
  [ORB_STATES.SPEAKING]: "Speaking",
  [ORB_STATES.ERROR]: "Connection error",
});

export async function mountTalkView({ outlet, signal }) {
  const pending = consumePendingApply();
  const micStatePromise = getMicState();
  let muted = true;
  let togglePending = false;
  let activePersonality = null;

  const caption = h("p", { class: "talk__caption" }, CAPTION_BY_STATE[ORB_STATES.CONNECTING]);
  const defaultAction = document.querySelector('[data-component="default-personality-action"]');
  if (defaultAction) {
    defaultAction.hidden = true;
    defaultAction.addEventListener("click", onSetDefault);
  }
  const orb = createOrb({
    initialState: ORB_STATES.CONNECTING,
    onStateChange: (state) => {
      caption.textContent = CAPTION_BY_STATE[state] || "";
    },
  });
  orb.root.addEventListener("click", onMicTap);
  syncMicAria();

  const composer = buildTextComposer();

  const view = h(
    "section",
    { class: "view view--talk" },
    h("div", { class: "talk__orb-wrap" }, orb.root),
    caption,
    composer.element
  );
  outlet.replaceChildren(view);

  if (pending) {
    caption.textContent = `Applying "${prettifyProfileName(pending.name)}"…`;
    try {
      await pending.promise;
    } catch (error) {
      if (signal.aborted) return;
      orb.setState(ORB_STATES.ERROR);
      caption.textContent = `Failed to apply personality: ${error?.message || error}`;
      return;
    }
    if (signal.aborted) return;
    // SSE "ready" will flip the orb to its resting state next tick.
    caption.textContent = CAPTION_BY_STATE[ORB_STATES.CONNECTING];
    void refreshPersonalityState();
  } else {
    // Deep link to /talk with no pending apply: refresh the header badge.
    void refreshPersonalityState();
  }

  try {
    muted = Boolean((await micStatePromise)?.muted);
  } catch {
    // keep the muted default
  }
  if (signal.aborted) return;
  syncMicAria();

  let everConnected = false;
  const subscription = subscribeConversationEvents({
    // Re-sync mic state on (re)connect: another tab may have toggled it.
    onReady: async () => {
      everConnected = true;
      if (!togglePending) {
        try {
          muted = Boolean((await getMicState())?.muted);
        } catch {
          // keep the last known mute state
        }
      }
      if (signal.aborted) return;
      orb.setState(restingState());
      caption.textContent = CAPTION_BY_STATE[restingState()];
      syncMicAria();
    },
    onActivity: (reason) => {
      if (muted) return;
      const next = mapActivityToState(reason);
      if (next == null) return;
      orb.setState(next);
    },
    onError: () => {
      // SSE auto-retries (e.g. 404 before routes exist), so a failure here is transient.
      orb.setState(ORB_STATES.CONNECTING);
      caption.textContent = everConnected ? "Reconnecting..." : CAPTION_BY_STATE[ORB_STATES.CONNECTING];
    },
  });

  signal.addEventListener("abort", () => {
    subscription.close();
    orb.dispose();
    if (defaultAction) {
      defaultAction.hidden = true;
      defaultAction.removeEventListener("click", onSetDefault);
    }
  });

  function restingState() {
    return muted ? ORB_STATES.MUTED : ORB_STATES.IDLE;
  }

  async function onMicTap() {
    if (togglePending) return;
    togglePending = true;
    try {
      const data = await setMicMuted(!muted);
      muted = Boolean(data?.muted);
    } catch (error) {
      if (!signal.aborted) {
        caption.textContent = `Failed to toggle the microphone: ${error?.message || error}`;
      }
      return;
    } finally {
      togglePending = false;
    }
    if (signal.aborted) return;
    orb.setState(restingState());
    // setState skips unchanged states, so set the caption explicitly
    caption.textContent = CAPTION_BY_STATE[restingState()];
    syncMicAria();
  }

  async function refreshPersonalityState() {
    const personalityState = await fetchPersonalityState();
    if (signal.aborted || personalityState == null) return;
    activePersonality = personalityState.current;
    setPersonality(personalityState.badgeName);
    const shouldHide = personalityState.locked || personalityState.current === personalityState.startup;
    if (defaultAction) {
      defaultAction.hidden = shouldHide;
    }
  }

  async function onSetDefault() {
    if (!defaultAction || !activePersonality) return;
    defaultAction.disabled = true;
    caption.textContent = `Saving "${displayPersonalityName(activePersonality)}" as default...`;
    try {
      await applyPersonality(activePersonality, { persist: true });
      if (signal.aborted) return;
      defaultAction.hidden = true;
      caption.textContent = `"${displayPersonalityName(activePersonality)}" will be used at startup.`;
    } catch (error) {
      if (!signal.aborted) {
        caption.textContent = `Failed to save default: ${error?.message || error}`;
      }
    } finally {
      defaultAction.disabled = false;
    }
  }

  function syncMicAria() {
    orb.root.setAttribute("aria-pressed", String(!muted));
    orb.root.setAttribute("aria-label", muted ? "Unmute microphone" : "Mute microphone");
  }
}

function displayPersonalityName(name) {
  return name === BUILT_IN_DEFAULT_OPTION ? "Default" : prettifyProfileName(name);
}

/**
 * Build the text composer: send a message to Reachy without speaking, optionally
 * making it repeat the text back verbatim. Posts to the /say backend endpoint.
 */
function buildTextComposer() {
  const input = h("textarea", {
    class: "talk__composer-input",
    rows: "2",
    placeholder: "Type something for Reachy to say…",
    "aria-label": "Message for Reachy",
  });
  const verbatim = h("input", { type: "checkbox", class: "talk__composer-checkbox" });
  const sendBtn = h("button", { type: "submit", class: "btn btn--primary" }, "Send");
  const status = h("p", { class: "talk__composer-status", role: "status", "aria-live": "polite" });

  const element = h(
    "form",
    { class: "talk__composer" },
    input,
    h(
      "div",
      { class: "talk__composer-row" },
      h(
        "label",
        { class: "talk__composer-mode" },
        verbatim,
        h("span", {}, "Say it back word for word")
      ),
      sendBtn
    ),
    status
  );

  function setStatus(message, isError = false) {
    status.textContent = message;
    status.classList.toggle("is-error", isError);
  }

  async function submit() {
    const text = (input.value || "").trim();
    if (!text) {
      setStatus("Type a message first.", true);
      return;
    }
    sendBtn.disabled = true;
    setStatus("Sending…");
    try {
      await sendText(text, verbatim.checked);
      input.value = "";
      setStatus(verbatim.checked ? "Sent — Reachy will say it." : "Sent.");
    } catch (error) {
      setStatus(`Failed: ${describeError(error)}`, true);
    } finally {
      sendBtn.disabled = false;
    }
  }

  element.addEventListener("submit", (event) => {
    event.preventDefault();
    void submit();
  });
  // Enter sends; Shift+Enter inserts a newline.
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  });

  return { element };
}

async function fetchPersonalityState() {
  try {
    const data = await listPersonalities();
    const current = data?.current;
    if (!current) return null;
    return {
      current,
      startup: data?.startup || BUILT_IN_DEFAULT_OPTION,
      locked: Boolean(data?.locked),
      badgeName: current === BUILT_IN_DEFAULT_OPTION ? "default" : current,
    };
  } catch {
    return null;
  }
}

const SSE_RECONNECT_MS = 2000;

function subscribeConversationEvents({ onActivity, onReady, onError } = {}) {
  if (typeof onActivity !== "function") {
    throw new TypeError("subscribeConversationEvents: onActivity is required");
  }

  let source = null;
  let retryTimer = null;
  let closed = false;

  function connect() {
    source = new EventSource(SSE_ENDPOINT);

    source.addEventListener("activity", (ev) => {
      const reason = (ev.data || "").trim();
      if (reason) onActivity(reason);
    });

    source.addEventListener("ready", () => onReady());

    source.addEventListener("error", (err) => {
      onError(err);
      // EventSource gives up on HTTP errors (e.g. 404 while the backend is
      // still registering routes); recreate it until the route exists.
      if (!closed && source.readyState === EventSource.CLOSED) {
        retryTimer = setTimeout(connect, SSE_RECONNECT_MS);
      }
    });
  }

  connect();

  return {
    close() {
      closed = true;
      if (retryTimer != null) clearTimeout(retryTimer);
      source.close();
    },
  };
}
