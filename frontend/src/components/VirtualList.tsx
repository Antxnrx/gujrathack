import { useVirtualizer } from "@tanstack/react-virtual";
import { useRef, type ReactNode } from "react";

/**
 * Windowed rows.
 *
 * The timeline pages at 300 and the detection tables run to tens of thousands; the
 * measured cost of *not* doing this is a browser laying out every row it has ever
 * fetched on each keystroke in the filter box. `overscan` is 8 because a control-room
 * operator scrolls in flicks, not in pixels, and 8 rows of pre-render is the difference
 * between a blank band and none.
 *
 * `estimateSize` is a hint, and `measureElement` corrects it per row — rows here vary
 * (a tagged event with a note is taller than a bare one), so a fixed height would drift
 * the scrollbar over a few hundred rows.
 */
export function VirtualList<T>({ items, estimate = 52, render, style, className }: {
  items: T[];
  estimate?: number;
  render: (item: T, index: number) => ReactNode;
  style?: React.CSSProperties;
  className?: string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const v = useVirtualizer({
    count: items.length,
    getScrollElement: () => host.current,
    estimateSize: () => estimate,
    overscan: 8,
  });

  return (
    <div ref={host} className={`vscroll ${className ?? ""}`} style={style}>
      <div style={{ height: v.getTotalSize(), position: "relative" }}>
        {v.getVirtualItems().map((row) => (
          <div key={row.key} data-index={row.index} ref={v.measureElement}
               style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${row.start}px)` }}>
            {render(items[row.index], row.index)}
          </div>
        ))}
      </div>
    </div>
  );
}
