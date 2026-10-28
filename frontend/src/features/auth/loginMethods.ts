import type { LoginIdentities } from "../../shared/api/auth";

export function linkedLoginMethods(data: LoginIdentities) {
  const linkedSlugs = new Set(data.identities.map((identity) => identity.provider_slug));
  return data.available_providers.filter((provider) => linkedSlugs.has(provider.slug));
}
