// Adapter for the pinned lidar 0.21 manager. Keep its octree loading and
// picking metadata, but replace CPU recolouring/filtering with GPU lookups.
import {PointCloudLayer} from '@deck.gl/layers';
import {COORDINATE_SYSTEM} from '@deck.gl/core';
import {LidarControl as BaseControl, PointCloudManager, getColormap,
        getClassificationColor} from 'maplibre-gl-lidar';
import {sampledBounds, colorLookup, classLookup} from './pointcloud-data.mjs';

const block = `layout(std140) uniform cloudStyleUniforms {
  vec2 colorRange;
  vec2 elevationRange;
  float scheme;
  float zOffset;
} cloudStyle;
uniform sampler2D cloudRamp;
uniform sampler2D cloudClasses;
`;
const module = {
  name: 'cloudStyle', vs: block, fs: block,
  uniformTypes: {colorRange: 'vec2<f32>', elevationRange: 'vec2<f32>', scheme: 'f32', zOffset: 'f32'},
};

export class GpuPointCloudLayer extends PointCloudLayer {
  static layerName = 'GpuPointCloudLayer';
  static defaultProps = {
    ...PointCloudLayer.defaultProps,
    getIntensity: {type: 'accessor', value: 0},
    getClassification: {type: 'accessor', value: 0},
  };
  getShaders() {
    const shaders = super.getShaders();
    return {...shaders, modules: [...shaders.modules, module], inject: {
      ...shaders.inject,
      'vs:#decl': 'in float instanceIntensity; in float instanceClassification; out float cloudVisible;',
      'fs:#decl': 'in float cloudVisible;',
      'vs:DECKGL_FILTER_GL_POSITION': `
        vec3 cloudPosition = geometry.worldPosition + vec3(0., 0., cloudStyle.zOffset);
        position = project_position_to_clipspace(cloudPosition, vec3(0.), vec3(0.), geometry.position);
      `,
      'vs:DECKGL_FILTER_COLOR': `
        vec4 cls = texelFetch(cloudClasses, ivec2(int(clamp(instanceClassification, 0., 255.)), 0), 0);
        cloudVisible = cls.a;
        if (geometry.worldPosition.z < cloudStyle.elevationRange.x || geometry.worldPosition.z > cloudStyle.elevationRange.y) cloudVisible = 0.;
        if (cloudStyle.scheme == 2.) color.rgb = cls.rgb;
        else if (cloudStyle.scheme != 3.) {
          float value = cloudStyle.scheme == 1. ? instanceIntensity : geometry.worldPosition.z;
          float t = clamp((value - cloudStyle.colorRange.x) / max(cloudStyle.colorRange.y - cloudStyle.colorRange.x, 0.000001), 0., 1.);
          color.rgb = texture(cloudRamp, vec2((t * 255. + 0.5) / 256., 0.5)).rgb;
        }
      `,
      'fs:DECKGL_FILTER_COLOR': 'if (cloudVisible < 0.5) discard;',
    }};
  }
  initializeState() {
    super.initializeState();
    this.getAttributeManager().addInstanced({
      instanceIntensity: {size: 1, accessor: 'getIntensity'},
      instanceClassification: {size: 1, accessor: 'getClassification'},
    });
  }
  updateState(params) {
    super.updateState(params);
    for (const [prop, key] of [['ramp', 'rampTexture'], ['classes', 'classTexture']]) {
      if (!this.state[key] || params.props[prop] !== params.oldProps[prop]) {
        this.state[key]?.destroy();
        this.state[key] = this.context.device.createTexture({
          width: 256, height: 1, format: 'rgba8unorm', data: params.props[prop],
          sampler: {minFilter: 'linear', magFilter: 'linear', addressModeU: 'clamp-to-edge', addressModeV: 'clamp-to-edge'},
        });
      }
    }
  }
  draw(params) {
    this.state.model.shaderInputs.setProps({cloudStyle: this.props.cloudStyle});
    this.state.model.setBindings({cloudRamp: this.state.rampTexture, cloudClasses: this.state.classTexture});
    super.draw(params);
  }
  finalizeState(context) {
    this.state.rampTexture?.destroy();
    this.state.classTexture?.destroy();
    super.finalizeState(context);
  }
}

export class GpuPointCloudManager extends PointCloudManager {
  constructor(overlay, options) {
    super(overlay, options);
    this._gpuData = new WeakMap();
    this._gpuDirty = new Set();
    // The inherited loaders ask for colors on each point batch. Only compute
    // bounded statistics here; no point-sized colour buffers are produced.
    this._colorProcessor = {getColorsWithBounds: (data, scheme, opts) => ({
      colors: new Uint8Array(0), bounds: this._bounds(data, scheme, opts),
    })};
  }
  _cached(data) {
    let cached = this._gpuData.get(data);
    if (!cached) { cached = {bounds: new Map(), chunks: []}; this._gpuData.set(data, cached); }
    return cached;
  }
  _bounds(data, scheme, options) {
    const key = JSON.stringify([scheme === 'intensity', options.usePercentile, options.colorRange]);
    const cache = this._cached(data).bounds;
    if (!cache.has(key)) cache.set(key, sampledBounds(data, scheme, options));
    return cache.get(key);
  }
  updateStyle(options) {
    this._options = {...this._options, ...options};
    for (const pc of this._pointClouds.values())
      this._lastComputedBounds = this._bounds(pc.data, this._options.colorScheme, this._options);
    this._updateAllLayers();
  }
  _createLayer(id) {
    this._gpuDirty.add(id);
    if (!this._gpuFrame) this._gpuFrame = requestAnimationFrame(() => {
      this._gpuFrame = 0;
      const ids = [...this._gpuDirty]; this._gpuDirty.clear();
      for (const key of ids) this._drawCloud(key);
    });
  }
  _drawCloud(id) {
    const pc = this._pointClouds.get(id);
    if (!pc) return;
    const {data, coordinateOrigin} = pc, opts = this._options;
    const cached = this._cached(data), chunkSize = 1000000;
    const count = Math.ceil(data.pointCount / chunkSize);
    const rampKey = opts.colormap ?? 'viridis';
    if (this._rampKey !== rampKey) { this._rampKey = rampKey; this._ramp = colorLookup(getColormap(rampKey)); }
    const classKey = JSON.stringify([...(opts.hiddenClassifications ?? [])].sort((a,b) => a-b)) + JSON.stringify(opts.classificationStyles);
    if (this._classKey !== classKey) {
      this._classKey = classKey;
      this._classes = classLookup(opts.hiddenClassifications ?? new Set(), c => getClassificationColor(c, opts.classificationStyles));
    }
    const bounds = this._bounds(data, opts.colorScheme, opts);
    for (let chunk = count; chunk < pc.chunkCount; chunk++) this._deckOverlay.removeLayer(`pointcloud-${id}-chunk${chunk}`);
    for (let chunk = 0; chunk < count; chunk++) {
      const start = chunk * chunkSize, length = Math.min(chunkSize, data.pointCount - start);
      if (!cached.chunks[chunk]) {
        const attributes = {getPosition: {value: data.positions.subarray(start * 3, (start + length) * 3), size: 3}};
        if (data.colors && data.hasRGB) attributes.getColor = {value: data.colors.subarray(start * 4, (start + length) * 4), size: 4};
        if (data.intensities) attributes.getIntensity = {value: data.intensities.subarray(start, start + length), size: 1};
        if (data.classifications) attributes.getClassification = {value: data.classifications.subarray(start, start + length), size: 1};
        cached.chunks[chunk] = {length, attributes};
      }
      const point = info => {
        if (!info.picked || info.index < 0 || info.index >= length) return null;
        const index = start + info.index;
        const result = {pointCloudId: id, index, longitude: coordinateOrigin[0] + data.positions[index * 3],
          latitude: coordinateOrigin[1] + data.positions[index * 3 + 1], elevation: data.positions[index * 3 + 2] + (opts.zOffset ?? 0), x: info.x, y: info.y};
        if (data.intensities) result.intensity = data.intensities[index];
        if (data.classifications) result.classification = data.classifications[index];
        if (data.colors && data.hasRGB) [result.red, result.green, result.blue] = data.colors.subarray(index * 4, index * 4 + 3);
        if (data.extraAttributes) result.attributes = Object.fromEntries(Object.entries(data.extraAttributes).filter(([,a]) => index < a.length).map(([k,a]) => [k,a[index]]));
        return result;
      };
      const layerId = `pointcloud-${id}-chunk${chunk}`;
      this._deckOverlay.addLayer(layerId, new GpuPointCloudLayer({
        id: layerId, data: cached.chunks[chunk], coordinateOrigin, coordinateSystem: COORDINATE_SYSTEM.LNGLAT_OFFSETS,
        pointSize: opts.pointSize, sizeUnits: 'pixels', material: false,
        visible: pc.visible, opacity: pc.opacityOverride ?? opts.opacity, pickable: opts.pickable,
        getColor: [255,255,255,255], ramp: this._ramp, classes: this._classes,
        cloudStyle: {colorRange: [bounds.min, bounds.max], elevationRange: opts.elevationRange ?? [-1e30,1e30],
          scheme: {elevation:0,intensity:1,classification:2,rgb:3}[opts.colorScheme] ?? 0, zOffset: opts.zOffset ?? 0},
        onHover: info => opts.onHover?.(point(info)), onClick: info => {const p = point(info); if (p) opts.onClick?.(p);},
      }));
    }
    this._pointClouds.set(id, {...pc, chunkCount: count});
  }
  clear() {
    if (this._gpuFrame) cancelAnimationFrame(this._gpuFrame);
    this._gpuFrame = 0; this._gpuDirty.clear();
    super.clear();
  }
}

export class LidarControl extends BaseControl {
  onAdd(map) {
    const element = super.onAdd(map);
    this._pointCloudManager = new GpuPointCloudManager(this._deckOverlay, this._pointCloudManager.getOptions());
    this._pointerOverCloud = false;
    this._cloudPointerMove = event => {
      this._pointerOverCloud = event.target?.tagName === 'CANVAS' && this._mapContainer.contains(event.target);
      if (!this._pointerOverCloud) this._handlePointHover(null);
    };
    this._cloudPointerLeave = () => { this._pointerOverCloud = false; this._handlePointHover(null); };
    document.addEventListener('pointermove', this._cloudPointerMove, true);
    document.addEventListener('pointerleave', this._cloudPointerLeave);
    map.on('movestart', this._cloudPointerLeave);
    return element;
  }
  _handlePointHover(info) {
    // Ignore a queued picking result after the pointer entered an overlay.
    super._handlePointHover(this._pointerOverCloud ? info : null);
  }
  onRemove() {
    document.removeEventListener('pointermove', this._cloudPointerMove, true);
    document.removeEventListener('pointerleave', this._cloudPointerLeave);
    this._map?.off('movestart', this._cloudPointerLeave);
    super.onRemove();
  }
  setHiddenClassifications(codes) {
    const hidden = new Set(codes);
    this._pointCloudManager.setHiddenClassifications(hidden);
    this.setState({hiddenClassifications: hidden});
  }
}
