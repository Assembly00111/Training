// Decrypts a plan the dashboard uploaded (imports/request.json).
// The page encrypts the .md in the browser with AES-256-GCM and wraps that key with
// the repository's RSA public key (config.json "import_public_key"). Only this job,
// holding the private key secret PLAN_IMPORT_PRIVATE_KEY, can read it.
// Usage: node decrypt_plan.mjs <request.json> <out.md> <out-id.txt>
import { readFileSync, writeFileSync } from "node:fs";
import { createPrivateKey, privateDecrypt, createDecipheriv, constants } from "node:crypto";

const [req, outMd, outId] = process.argv.slice(2);
const r = JSON.parse(readFileSync(req, "utf8"));
writeFileSync(outId, String(r.id || ""));
const pk = (process.env.PLAN_IMPORT_PRIVATE_KEY || "").replace(/\s+/g, "");
if (!pk) { console.error("PLAN_IMPORT_PRIVATE_KEY secret is missing."); process.exit(2); }
const key = privateDecrypt(
  { key: createPrivateKey({ key: Buffer.from(pk, "base64"), format: "der", type: "pkcs8" }),
    padding: constants.RSA_PKCS1_OAEP_PADDING, oaepHash: "sha256" },
  Buffer.from(r.key, "base64"));
const data = Buffer.from(r.data, "base64");
const d = createDecipheriv("aes-256-gcm", key, Buffer.from(r.iv, "base64"));
d.setAuthTag(data.subarray(data.length - 16));
writeFileSync(outMd, Buffer.concat([d.update(data.subarray(0, data.length - 16)), d.final()]));
console.log("Plan decrypted.");
