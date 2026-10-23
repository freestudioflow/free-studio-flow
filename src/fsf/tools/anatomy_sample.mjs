import { readFileSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [checkout, jobPath, outPath] = process.argv.slice(2);
const job = JSON.parse(readFileSync(jobPath, "utf8"));
if (job.provenance !== "model_sample") {
  process.stderr.write("MECHANISM_PROVENANCE\n");
  process.exit(2);
}
const num = job.clock?.num;
const den = job.clock?.den;
if (!((num === 24 && den === 1) || (num === 24000 && den === 1001))) {
  process.stderr.write("MECHANISM_CLOCK\n");
  process.exit(2);
}
const { springStates } = await import(pathToFileURL(`${checkout}/kit/audit.mjs`));
const states = springStates(job.order, {
  stagger: job.stagger,
  stiffness: job.stiffness,
  damping: job.damping,
  substeps: job.substeps,
  fps: num / den,
  settle: 5e-4,
  stride: 1,
});
if (states.length < 1 || states.length > 96) {
  process.stderr.write("MECHANISM_RANGE\n");
  process.exit(2);
}
const frames = states.map((state, frame) => {
  const pose = { shares: state.shares };
  return { frame, pose, readout: { shares: pose.shares } };
});
writeFileSync(outPath, JSON.stringify({
  provenance: "model_sample",
  anatomy_commit: job.expected_commit,
  clock: { num, den },
  frames,
  motion_review: "not_reviewed",
}));
