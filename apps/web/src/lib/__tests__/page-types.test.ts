import { describe, expect, it } from "bun:test"

import { displayPageType, PAGE_TYPES, pageRole, pageType } from "@/lib/page-types"
import { profileHref } from "@/lib/profile-href"
import { profileName } from "@/lib/profile-name"

describe("page types", () => {
  it("preserves all backend page types", () => {
    expect(PAGE_TYPES.map(pageType)).toEqual([
      "club",
      "collective",
      "project",
      "festival",
    ])
  })

  it("keeps the existing club fallback for unknown data", () => {
    expect(pageType("unknown")).toBe("club")
    expect(pageType(null)).toBe("club")
  })

  it("uses neutral labels for unknown management data", () => {
    expect(displayPageType("future-type")).toBe("unknown")
    expect(pageRole("future-role")).toBe("unknown")
    expect(displayPageType("festival")).toBe("festival")
    expect(pageRole("owner")).toBe("owner")
  })

  it("routes every page type through the public Page boundary", () => {
    expect(PAGE_TYPES.map((type) => profileHref({
      type,
      handle: `${type}-slug`,
    }))).toEqual([
      "/pages/club-slug",
      "/pages/collective-slug",
      "/pages/project-slug",
      "/pages/festival-slug",
    ])
  })

  it("does not link erased attribution to a real user with the sentinel handle", () => {
    const erased = {
      id: "deleted-user", type: "consumer" as const, handle: "deleted-user", displayName: "Deleted User",
    }
    expect(profileName(erased)).toBe("Deleted User")
    expect(profileHref(erased)).toBeNull()
    const active = { ...erased, id: "active-user-id" }
    expect(profileName(active)).toBe("deleted-user")
    expect(profileHref(active)).toBe("/u/deleted-user")
  })
})
