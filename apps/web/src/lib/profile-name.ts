import type { ProfileRef } from "@/lib/types"

export function isErasedAuthor(profile: { id?: string }): boolean {
  return profile.id === "deleted-user"
}

/**
 * One public name per person: the unique registration username, which is what
 * `handle` holds for people. Pages are entities with a proper name, and their
 * `handle` is a slug — not something to show a reader.
 */
export function profileName(profile: ProfileRef): string {
  if (isErasedAuthor(profile)) return profile.displayName
  return profile.type === "artist" || profile.type === "consumer"
    ? profile.handle
    : profile.displayName
}
