import { useEffect, useRef, useState } from "react";
import type { DetectionRow } from "../lib/types";

/**
 * What the detector saw, drawn over the frame it saw it in.
 *
 * Geometry
 * --------
 * Boxes are stored in *source pixels* alongside the frame they were measured
 * against (`frame_w` × `frame_h`, 854 × 480 on this estate). The video is laid
 * out with `object-fit: contain`, so it letterboxes inside its box and the
 * drawn picture is almost never the same rectangle as the element. Placing a
 * box therefore means reproducing `contain` in arithmetic: scale by the smaller
 * ratio, centre the remainder, then offset every coordinate by that margin.
 *
 * The alternative — an SVG with `preserveAspectRatio="none"` stretched across
 * the element — is what `ZoneOverlay` does, and it is wrong for exactly this
 * reason: it maps the frame onto the element rather than onto the picture, so
 * every shape sits off by the size of the letterbox.
 *
 * Boxes and labels are HTML rather than SVG so the text does not inherit the
 * viewBox scale. A label that grows with the frame is unreadable on a wall tile
 * and enormous on a stage.
 *
 * What these boxes are, and are not
 * ---------------------------------
 * They are detections this camera's worker genuinely produced. They are **not**
 * an annotation of the frame currently on screen, unless the source is a true
 * live edge. This estate's gateway serves a 24-hour seekable archive: the worker
 * walks its segments from the start while the browser plays from wherever it
 * seeked to, so the two are independent playheads over the same recording.
 * Aligning them requires each detection to carry its absolute offset into the
 * archive, which is not stored yet — so the caller turns this on deliberately
 * and the control that does so says what it is showing.
 *
 * Honesty about lag
 * -----------------
 * These are polled, not streamed, so a box is always a little behind the
 * picture. Rather than draw a stale box as though it were current, each fades
 * over `WINDOW_S` and then leaves. An operator can see that a box is ageing,
 * which is the truthful rendering of "one second behind" — and a frozen box on
 * a moving road is the single most misleading thing this view could show.
 */

/** How long a detection stays on screen before it has faded out entirely. */
const WINDOW_S = 4;

/** Below this the box is not drawn at all — past it a box is noise, not signal. */
const MIN_OPACITY = 0.15;

type Fit = { scale: number; offX: number; offY: number };

/** Reproduce `object-fit: contain` for a frame inside a box. */
function fit(boxW: number, boxH: number, frameW: number, frameH: number): Fit {
  const scale = Math.min(boxW / frameW, boxH / frameH);
  return {
    scale,
    offX: (boxW - frameW * scale) / 2,
    offY: (boxH - frameH * scale) / 2,
  };
}

/**
 * Three colours, three meanings — not one per COCO class.
 *
 * A rainbow overlay makes an operator decode a legend before they can read the
 * road. What actually changes a decision is whether the thing is a person,
 * and whether we came away with a plate, so those are the only distinctions
 * drawn: green means this one produced a registration.
 */
function tone(d: DetectionRow): "plate" | "person" | "vehicle" {
  if (d.plate_text) return "plate";
  if (d.class === "person") return "person";
  return "vehicle";
}

export function DetectionOverlay({ detections }: { detections: DetectionRow[] }) {
  const host = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState({ w: 0, h: 0 });
  const [now, setNow] = useState(() => Date.now());

  // The fade needs a clock of its own, so a caller can hand over rows and
  // nothing else. It runs only while there is something to fade — an overlay on
  // an idle camera should not wake the main thread ten times a second forever.
  useEffect(() => {
    if (detections.length === 0) return;
    const t = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(t);
  }, [detections.length]);

  // The element resizes on fullscreen, on a density change and on any window
  // drag. Measuring once on mount puts every box in the wrong place for the
  // rest of the session.
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => {
      const r = e.contentRect;
      setBox({ w: r.width, h: r.height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return (
    <div className="detlay" ref={host} aria-hidden="true">
      {box.w > 0 && detections.map((d) => {
        // A detection with no frame dimensions cannot be placed: the numbers in
        // `bbox` are pixels in a frame of unknown size. Guessing a resolution
        // would draw a box in the wrong place and look authoritative doing it.
        if (!d.frame_w || !d.frame_h || !d.bbox) return null;

        const age = (now - new Date(d.ts).getTime()) / 1000;
        const opacity = 1 - age / WINDOW_S;
        if (opacity < MIN_OPACITY) return null;

        const f = fit(box.w, box.h, d.frame_w, d.frame_h);
        const { x1, y1, x2, y2 } = d.bbox;
        const style = {
          left:   f.offX + x1 * f.scale,
          top:    f.offY + y1 * f.scale,
          width:  (x2 - x1) * f.scale,
          height: (y2 - y1) * f.scale,
          opacity,
        };

        return (
          <div key={d.id} className={`detbox ${tone(d)}`} style={style}>
            <span className="detbox-tag">{d.class}</span>
            {/* The plate is the payload, so it is set apart from the class tag:
                monospace, its own chip, below the box where it does not cover
                the vehicle the operator is trying to look at. */}
            {d.plate_text && <span className="detbox-plate">{d.plate_text}</span>}
          </div>
        );
      })}
    </div>
  );
}
