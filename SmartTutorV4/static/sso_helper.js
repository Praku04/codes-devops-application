/* global msal, getTokenFromBackend, hideLoader, showLoader */

let ssoConfig = null;
let msalInstance = null;

const OKTA_PKCE_VERIFIER_KEY = "okta_pkce_verifier";
const OKTA_PKCE_STATE_KEY = "okta_pkce_state";
const OKTA_SCOPES = "openid profile email";

const _base64UrlEncode = (bytes) => {
  let binary = "";
  const arr = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  for (let i = 0; i < arr.length; i += 1) {
    binary += String.fromCharCode(arr[i]);
  }
  // "=" only ever appears as trailing padding in base64, so removing every one strips the
  // padding without a backtracking regex (Sonar S5852).
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
};

const _randomUrlSafe = (byteLength) => {
  const bytes = new Uint8Array(byteLength);
  crypto.getRandomValues(bytes);
  return _base64UrlEncode(bytes);
};

const _sha256Base64Url = async (plain) => {
  const data = new TextEncoder().encode(plain);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return _base64UrlEncode(digest);
};

const _updateSsoButtonLabel = () => {
  const btnText = document.querySelector("#login-btn-sso .auth-btn-text");
  if (!btnText || !ssoConfig) return;
  const authType = (ssoConfig.auth_type || "").toLowerCase();
  if (authType === "okta") {
    btnText.textContent = "Sign in with Okta";
  } else {
    btnText.textContent = "Sign in with SSO";
  }
};

const _clearOktaPkce = () => {
  try {
    sessionStorage.removeItem(OKTA_PKCE_VERIFIER_KEY);
    sessionStorage.removeItem(OKTA_PKCE_STATE_KEY);
  } catch {
    /* sessionStorage may be unavailable */
  }
};

const _stripOidcParamsFromUrl = () => {
  try {
    const url = new URL(window.location.href);
    [
      "code",
      "state",
      "session_state",
      "error",
      "error_description",
      "iss",
    ].forEach((key) => url.searchParams.delete(key));
    window.history.replaceState({}, document.title, url.pathname + url.search + url.hash);
  } catch {
    /* ignore history failures */
  }
};

const _exchangeOktaCode = async (code, verifier) => {
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: ssoConfig.client_id,
    redirect_uri: window.location.origin,
    code,
    code_verifier: verifier,
  });
  const response = await fetch(ssoConfig.token_url, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      Accept: "application/json",
    },
    body,
  });
  if (!response.ok) {
    throw new Error("Okta token exchange failed");
  }
  const payload = await response.json();
  if (!payload || typeof payload.id_token !== "string" || !payload.id_token) {
    throw new Error("Okta response missing id_token");
  }
  return payload.id_token;
};

const _handleOktaRedirectCallback = async () => {
  const params = new URLSearchParams(window.location.search);
  const error = params.get("error");
  const code = params.get("code");
  const state = params.get("state");
  if (!error && !code) {
    return false;
  }

  const expectedState = sessionStorage.getItem(OKTA_PKCE_STATE_KEY);
  const verifier = sessionStorage.getItem(OKTA_PKCE_VERIFIER_KEY);
  _clearOktaPkce();
  _stripOidcParamsFromUrl();

  if (error) {
    console.error("Okta login error");
    return true;
  }
  if (!code || !state || !expectedState || state !== expectedState || !verifier) {
    console.error("Okta login state mismatch");
    return true;
  }
  try {
    if (typeof showLoader === "function") {
      showLoader("Logging in with SSO...");
    }
    const idToken = await _exchangeOktaCode(code, verifier);
    if (typeof getTokenFromBackend === "function") {
      await getTokenFromBackend(idToken);
    }
  } catch (err) {
    console.error("Okta callback failed:", err);
    if (typeof hideLoader === "function") {
      hideLoader();
    }
  }
  return true;
};

const _startOktaLoginRedirect = async () => {
  if (!ssoConfig?.authorize_url || !ssoConfig?.client_id) {
    throw new Error("Okta SSO is not configured");
  }
  const verifier = _randomUrlSafe(32);
  const state = _randomUrlSafe(16);
  const challenge = await _sha256Base64Url(verifier);
  sessionStorage.setItem(OKTA_PKCE_VERIFIER_KEY, verifier);
  sessionStorage.setItem(OKTA_PKCE_STATE_KEY, state);

  const url = new URL(ssoConfig.authorize_url);
  url.searchParams.set("client_id", ssoConfig.client_id);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("scope", OKTA_SCOPES);
  url.searchParams.set("redirect_uri", window.location.origin);
  url.searchParams.set("state", state);
  url.searchParams.set("code_challenge", challenge);
  url.searchParams.set("code_challenge_method", "S256");
  window.location.assign(url.toString());
};

const initMsal = async () => {
  // VAPT #10 (HttpOnly missing): MSAL's localStorage cache manager writes a
  // `msal.cache.encryption` cookie from JavaScript via document.cookie. A cookie written
  // that way can never carry HttpOnly - that attribute exists to hide a cookie FROM
  // JavaScript, so the browser will not let script set it. The cookie holds the AES key for
  // MSAL's own token cache; it is not this app's session (that is a Bearer JWT in
  // localStorage under `authToken`, sent via the Authorization header, never as a cookie).
  //
  // sessionStorage is the one supported cache location whose manager has no cookie write at
  // all, so the finding is closed by removing the cookie rather than by trying to add an
  // attribute the platform forbids. The server validates this value against a fixed
  // allow-list (features/general/sso_helper.py) before sending it.
  //
  // `storeAuthStateCookie` is deliberately NOT set: it is a v2-era option that this bundle
  // (@azure/msal-browser v5) ignores entirely, and leaving it in implied a cookie control
  // that was never active. MSAL's redirect-state cache is hard-coded to sessionStorage
  // regardless of this setting, so the login redirect round-trip is unaffected.
  const cacheLocation = ssoConfig.cache_location || "sessionStorage";
  msalInstance = new msal.PublicClientApplication({
    auth: {
      clientId: `${ssoConfig.client_id}`,
      authority: `https://login.microsoftonline.com/${ssoConfig.tenant_id}`,
      redirectUri: `${window.location.origin}`,
    },
    system: {
      asyncPopups: true,
    },
    cache: {
      cacheLocation: cacheLocation,
    },
  });
  await msalInstance.initialize();
  console.log("MSAL Initialized!");
  msalInstance
    .handleRedirectPromise()
    .then(async (response) => {
      if (response) {
        msalInstance.setActiveAccount(response.account);
        getTokenFromBackend(response.idToken);
      }
    })
    .catch(console.log);
};

const initOkta = async () => {
  console.log("Okta SSO ready");
  await _handleOktaRedirectCallback();
};

const initSSO = async () => {
  try {
    if (ssoConfig) return ssoConfig;
  } catch {
    console.log("Loading SSO Config");
  }
  const response = await fetch(`${window.location.origin}/auth/sso-config`, {
    method: "GET",
  });
  ssoConfig = await response.json();
  if (typeof hideLoader === "function") {
    hideLoader();
  }

  const authType = (ssoConfig.auth_type || "").toLowerCase();
  _updateSsoButtonLabel();

  if (authType === "msal") {
    await initMsal();
    return ssoConfig;
  }
  if (authType === "okta") {
    await initOkta();
    return ssoConfig;
  }
  console.log("Unsupported sso auth type");
  return ssoConfig;
};

window.onload = async () => {
  await initSSO();
};

const loginSSO = async () => {
  try {
    const authType = (ssoConfig?.auth_type || "").toLowerCase();
    if (authType === "okta") {
      await _startOktaLoginRedirect();
      return null;
    }
    if (authType !== "msal" || !msalInstance) {
      console.error("SSO is not initialized");
      return null;
    }
    const accounts = msalInstance.getAllAccounts();
    if (accounts.length === 1) {
      const account = accounts[0];
      msalInstance.setActiveAccount(account);
      try {
        const res = await msalInstance.acquireTokenSilent({
          scopes: ["openid", "profile", "email"],
          account: account,
        });
        return res.idToken;
      } catch {
        msalInstance.loginRedirect({
          scopes: ["openid", "profile", "email"],
        });
      }
    } else {
      await msalInstance.loginRedirect({
        scopes: ["openid", "profile", "email"],
        prompt: "select_account",
      });
    }
  } catch (err) {
    console.error("Login failed:", err);
  }
  return null;
};
