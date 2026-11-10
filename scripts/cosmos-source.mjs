import { createHash } from "node:crypto";

export const COSMOS_VERSION = "3.4.2";
export const COSMOS_SOURCE_SHA256 = "4ad56e0d6ddb6ad483baee7464b6ab0dd88b31c6af7f049408ad50574b8f1ff2";
export const sha256 = value => createHash("sha256").update(value).digest("hex");
