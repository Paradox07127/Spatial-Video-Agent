// Release resources owned by a removed subtree. Shared application materials remain alive.
export function releaseTree(group, keepMaterials = new Set()) {
  const geometries = new Set(), materials = new Set();
  group.traverse((object) => {
    if (object.isCSS2DObject) object.element.remove();
    if (object.geometry) geometries.add(object.geometry);
    for (const material of [object.material ?? []].flat()) {
      if (!keepMaterials.has(material)) materials.add(material);
    }
  });
  for (const geometry of geometries) geometry.dispose();
  for (const material of materials) material.dispose();
  // Textures are owned by the stage cache and released once when the stage closes.
  group.clear();
}
