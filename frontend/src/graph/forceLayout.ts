/** Fruchterman-Reingold-style force layout, hand-rolled instead of adding d3-force.
 * ponytail: naive O(n²) repulsion per iteration, fine at the spec's default node_limit (200) /
 * edge_limit (500) caps — swap to d3-force (quadtree Barnes-Hut) if graphs grow past that. */

export type LayoutPoint = { x: number; y: number };

export function computeLayout(
  nodeIds: string[],
  edges: { from: string; to: string }[],
  width: number,
  height: number,
  iterations = 220
): Map<string, LayoutPoint> {
  const area = width * height;
  const k = Math.sqrt(area / Math.max(nodeIds.length, 1)) * 0.9;
  const positions = new Map<string, LayoutPoint>();

  nodeIds.forEach((id, index) => {
    const angle = (index / Math.max(nodeIds.length, 1)) * Math.PI * 2;
    const radius = Math.min(width, height) * 0.32;
    positions.set(id, { x: width / 2 + radius * Math.cos(angle), y: height / 2 + radius * Math.sin(angle) });
  });

  const validEdges = edges.filter((edge) => positions.has(edge.from) && positions.has(edge.to));
  let temperature = width * 0.05;

  for (let step = 0; step < iterations; step++) {
    const displacement = new Map<string, LayoutPoint>(nodeIds.map((id) => [id, { x: 0, y: 0 }]));

    for (let i = 0; i < nodeIds.length; i++) {
      for (let j = i + 1; j < nodeIds.length; j++) {
        const a = positions.get(nodeIds[i])!;
        const b = positions.get(nodeIds[j])!;
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const force = (k * k) / dist;
        dx = (dx / dist) * force;
        dy = (dy / dist) * force;
        const da = displacement.get(nodeIds[i])!;
        const db = displacement.get(nodeIds[j])!;
        da.x += dx; da.y += dy;
        db.x -= dx; db.y -= dy;
      }
    }

    for (const edge of validEdges) {
      const a = positions.get(edge.from)!;
      const b = positions.get(edge.to)!;
      let dx = a.x - b.x;
      let dy = a.y - b.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const force = (dist * dist) / k;
      dx = (dx / dist) * force;
      dy = (dy / dist) * force;
      const da = displacement.get(edge.from)!;
      const db = displacement.get(edge.to)!;
      da.x -= dx; da.y -= dy;
      db.x += dx; db.y += dy;
    }

    for (const id of nodeIds) {
      const pos = positions.get(id)!;
      const disp = displacement.get(id)!;
      const centerPull = 0.02;
      disp.x += (width / 2 - pos.x) * centerPull;
      disp.y += (height / 2 - pos.y) * centerPull;
      const dist = Math.sqrt(disp.x * disp.x + disp.y * disp.y) || 0.01;
      const capped = Math.min(dist, temperature);
      pos.x += (disp.x / dist) * capped;
      pos.y += (disp.y / dist) * capped;
      pos.x = Math.max(24, Math.min(width - 24, pos.x));
      pos.y = Math.max(24, Math.min(height - 24, pos.y));
    }

    temperature *= 0.96;
  }

  return positions;
}
