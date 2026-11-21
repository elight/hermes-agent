// @vitest-environment jsdom
// The signed-out half of the `/api/dashboard/themes` gate.
//
// `GET /api/dashboard/themes` is no longer on the dashboard's public allowlist
// (hermes_cli/dashboard_auth/public_paths.py), so a browser that loads the SPA
// shell before signing in gets a 401 from the very first call ThemeProvider
// makes on mount. That 401 has to be the sign-in trigger, not a broken shell:
// this file pins the whole chain end to end through the REAL fetchJSON —
// gate envelope -> `window.location.assign(login_url)` -> the mounted tree
// still standing with a usable theme, so nothing renders as "no themes" and
// nothing throws on the way out.
//
// Deliberately NOT mocking @/lib/api: the redirect lives in fetchJSON, and a
// stubbed api client would test this file's own assumptions instead of the
// path the browser takes.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { ThemeProvider, useTheme } from "@/themes";

/** The gate's refusal body (`dashboard_auth/middleware.py`): `login_url` plus
 *  the `error: "unauthenticated"` code fetchJSON acts on. */
const LOGIN_URL = "/login?next=%2Fchat";

function unauthorizedEnvelope(): Response {
  return new Response(
    JSON.stringify({
      error: "unauthenticated",
      reason: "no_cookie",
      login_url: LOGIN_URL,
      providers: ["conduit"],
    }),
    { status: 401, headers: { "Content-Type": "application/json" } },
  );
}

function refuseThemes(): ReturnType<typeof vi.fn> {
  return vi.fn<typeof fetch>(async () => unauthorizedEnvelope());
}

let container: HTMLDivElement;
let root: Root;
let assign: ReturnType<typeof vi.fn>;

/** The origin this jsdom reports, so the stub Location is consistent with what
 *  `sameOriginRequestUrl` compares against (api-request-scope.ts:77 refuses a
 *  request whose resolved origin differs from `location.origin`). */
const ORIGIN = "http://localhost:3000";

/** jsdom's Location is unforgeable (its own `assign` is non-writable, and
 *  vitest's `stubGlobal` is the supported way to swap the whole object — the
 *  desktop suite uses the same seam). In jsdom `window === globalThis`, so
 *  stubbing `location` is what `window.location` reads back. It has to carry an
 *  absolute `href` + `origin` (the same-origin guard needs both) and the
 *  `pathname` + `search` fetchJSON saves for the post-login return. */
function stubLocation(pathname = "/chat", search = "") {
  assign = vi.fn();
  vi.stubGlobal("location", {
    assign,
    pathname,
    search,
    origin: ORIGIN,
    href: `${ORIGIN}${pathname}${search}`,
  });
}

/** This jsdom runs without an origin, so `localStorage` / `sessionStorage` can
 *  be undefined while ThemeProvider and fetchJSON read them — same stub
 *  `SidebarTargetSize.test.tsx` and `ChatPage.test.tsx` install. Both are
 *  needed here: the provider seeds the theme from one, fetchJSON writes the
 *  return-to path into the other. */
function memoryStore() {
  const store = new Map<string, string>();
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, String(value)),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
  };
}

/** Stand-in for the app shell: renders, and reports what the theme context
 *  handed it, so "the shell still works" is a measurement, not a claim. */
function Shell() {
  const { themeName, theme, availableThemes } = useTheme();
  return (
    <div>
      <span id="mounted">shell-mounted</span>
      <span id="theme-name">{themeName}</span>
      <span id="palette">{theme.palette.background.hex}</span>
      <span id="theme-count">{availableThemes.length}</span>
    </div>
  );
}

function renderShell() {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  return act(async () => {
    root.render(
      <ThemeProvider>
        <Shell />
      </ThemeProvider>,
    );
  });
}

beforeEach(() => {
  // React 19 only lets act() drive updates when the environment declares
  // itself; without this it warns "not configured to support act(...)" the
  // moment a promise settles outside the act scope.
  (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

  // Gated mode with no session: exactly what the pre-login shell has. The
  // token is absent (the server injects it only into an authenticated render)
  // and __HERMES_AUTH_REQUIRED__ is true, so fetchJSON takes the sign-in
  // redirect branch rather than the loopback stale-token reload branch.
  Object.defineProperty(window, "__HERMES_SESSION_TOKEN__", {
    configurable: true,
    writable: true,
    value: undefined,
  });
  Object.defineProperty(window, "__HERMES_AUTH_REQUIRED__", {
    configurable: true,
    writable: true,
    value: true,
  });

  stubLocation();
  vi.stubGlobal("localStorage", memoryStore());
  vi.stubGlobal("sessionStorage", memoryStore());
});

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ThemeProvider against a gated /api/dashboard/themes", () => {
  it("navigates the browser to the gate's login_url", async () => {
    const fetchMock = refuseThemes();
    vi.stubGlobal("fetch", fetchMock);

    await renderShell();

    await vi.waitFor(() => expect(assign).toHaveBeenCalledWith(LOGIN_URL));

    // The themes route is asked exactly once — ThemeProvider does not retry a
    // refused route. (The mount also reads the font preference, so the total
    // fetch count is more than one; that is the other pre-shell read.)
    const themeRequests = fetchMock.mock.calls.filter(([req]) =>
      String(req).includes("/api/dashboard/themes"),
    );
    expect(themeRequests).toHaveLength(1);
  });

  it("leaves the mounted shell standing and usable, not a broken page", async () => {
    vi.stubGlobal("fetch", refuseThemes());

    await renderShell();
    await vi.waitFor(() => expect(assign).toHaveBeenCalled());

    // Still mounted, and the context still resolves to a real built-in theme,
    // so what is on screen while the browser navigates is the shell — not an
    // empty theme picker, not an error state, not an unmounted root.
    expect(container.querySelector("#mounted")?.textContent).toBe("shell-mounted");
    expect(container.querySelector("#theme-name")?.textContent).toBe("default");
    expect(container.querySelector("#palette")?.textContent).toBeTruthy();
    expect(Number(container.querySelector("#theme-count")?.textContent)).toBeGreaterThan(0);

    // Where the user was heading survives the bounce, for the post-login return.
    // Literal, not read back off the stub, so a stub that lost the fields
    // cannot satisfy this by agreeing with itself.
    expect(window.sessionStorage.getItem("hermes.lastLocation")).toBe("/chat");
  });
});
