import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

/**
 * One-time recovery links -- password reset and email verification -- carry
 * their token in the URL fragment: `/reset-password#token=...`. A browser
 * never sends the fragment to the server, so the token stays out of the
 * access logs (uvicorn, Caddy, nginx) and Referer headers that the old
 * `?token=` form was written into. Links mailed before that change still use
 * the query string and keep working here.
 *
 * Either way the token is removed from the address bar as soon as the screen
 * reads it, and from then on lives only in component memory: it is submitted
 * in POST JSON bodies and never put back into a URL. Reloading the page
 * therefore starts without a token, which the reset screen answers by asking
 * for the link again rather than calling a still-valid link dead.
 */
const TOKEN_PARAMETER = "token";

type LinkLocation = { hash: string; search: string };

function fragmentParameters(hash: string) {
  return new URLSearchParams(hash.startsWith("#") ? hash.slice(1) : hash);
}

/** The token the link carries: the fragment first, then a legacy query. */
export function readOneTimeLinkToken(location: LinkLocation): string {
  const fromFragment = fragmentParameters(location.hash)
    .get(TOKEN_PARAMETER)
    ?.trim();
  if (fromFragment) {
    return fromFragment;
  }
  return (
    new URLSearchParams(location.search).get(TOKEN_PARAMETER)?.trim() ?? ""
  );
}

function carriesOneTimeLinkToken(location: LinkLocation): boolean {
  return (
    fragmentParameters(location.hash).has(TOKEN_PARAMETER) ||
    new URLSearchParams(location.search).has(TOKEN_PARAMETER)
  );
}

function searchWithoutToken(search: string): string {
  const parameters = new URLSearchParams(search);
  parameters.delete(TOKEN_PARAMETER);
  const rest = parameters.toString();
  return rest ? `?${rest}` : "";
}

function hashWithoutToken(hash: string): string {
  const parameters = fragmentParameters(hash);
  if (!parameters.has(TOKEN_PARAMETER)) {
    return hash;
  }
  parameters.delete(TOKEN_PARAMETER);
  const rest = parameters.toString();
  return rest ? `#${rest}` : "";
}

/**
 * Read the one-time token from the link that opened this screen, then take it
 * out of the address bar.
 *
 * The removal is a replace navigation through the router, which under the
 * app's browser router is `history.replaceState`: the entry the link created
 * is rewritten rather than followed by a new one, so Back does not lead to a
 * URL that still carries the token, and the router's own location -- which
 * other code turns into `?next=` targets -- no longer holds it either.
 */
export function useOneTimeLinkToken(): string {
  const location = useLocation();
  const navigate = useNavigate();
  const [token, setToken] = useState(() => readOneTimeLinkToken(location));
  const linkCarriesToken = carriesOneTimeLinkToken(location);

  useEffect(() => {
    if (!linkCarriesToken) {
      return;
    }
    // A second link opened over this screen (a same-document fragment
    // change) replaces the token the first one carried.
    const linkToken = readOneTimeLinkToken(location);
    if (linkToken) {
      setToken(linkToken);
    }
    void navigate(
      {
        pathname: location.pathname,
        search: searchWithoutToken(location.search),
        hash: hashWithoutToken(location.hash),
      },
      { replace: true, preventScrollReset: true, state: location.state },
    );
  }, [linkCarriesToken, location, navigate]);

  return token;
}
