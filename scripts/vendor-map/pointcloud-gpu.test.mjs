import {test} from 'node:test';
import assert from 'node:assert/strict';
import {GpuPointCloudManager} from './pointcloud-gpu.mjs';
import {sampledBounds, colorLookup, classLookup} from './pointcloud-data.mjs';

test('lookups preserve ramp endpoints and hide all 256 class codes', () => {
  const ramp = colorLookup([[0,0,0], [255,128,64]]);
  assert.deepEqual([...ramp.slice(0,4)], [0,0,0,255]);
  assert.deepEqual([...ramp.slice(-4)], [255,128,64,255]);
  const classes = classLookup(new Set([2,255]), () => [10,20,30]);
  assert.equal(classes[2 * 4 + 3], 0);
  assert.equal(classes[255 * 4 + 3], 0);
  assert.equal(classes[64 * 4 + 3], 255);
});

test('range sampling is bounded and respects explicit ranges', () => {
  let reads = 0;
  const positions = new Proxy({}, {get: (_, k) => {reads++; return Number(k) / 3;}});
  const data = {pointCount: 10000000, positions, bounds: {minZ:0,maxZ:10000000}};
  const auto = sampledBounds(data, 'elevation');
  assert.equal(reads, 16384);
  assert.ok(auto.min < auto.max);
  assert.deepEqual(sampledBounds(data, 'elevation', {colorRange:{mode:'absolute',absoluteMin:10,absoluteMax:20}}), {min:10,max:20});
});

test('style changes share binary buffers, coalesce, and preserve picking', () => {
  let pending, draws = 0, picked;
  globalThis.requestAnimationFrame = fn => {pending = fn; return 1;};
  globalThis.cancelAnimationFrame = () => {pending = undefined;};
  const layers = new Map();
  const manager = new GpuPointCloudManager({
    addLayer: (id, layer) => {draws++; layers.set(id,layer);},
    removeLayer: id => layers.delete(id),
  }, {onHover: p => {picked = p;}});
  const data = {positions:new Float32Array([0,0,100,1,1,150]), coordinateOrigin:[-123,44,0],
    pointCount:2, bounds:{minZ:100,maxZ:150}, hasClassification:true,
    classifications:new Uint8Array([2,64]), intensities:new Float32Array([20,40]),
    extraAttributes:{returnNumber:new Uint8Array([1,2])}};
  manager.addPointCloud('cloud',data);
  pending();
  const before = layers.values().next().value.props.data;
  manager.setColormap('plasma');
  manager.setHiddenClassifications(new Set([2]));
  manager.setElevationRange([120,160]);
  manager.setZOffset(-100);
  assert.equal(draws,1, 'all changes wait for one frame');
  pending();
  const layer = layers.values().next().value;
  assert.equal(draws,2);
  assert.equal(layer.props.data,before);
  assert.equal(before.attributes.getPosition.value.buffer,data.positions.buffer);
  assert.equal(layer.props.classes[2 * 4 + 3],0);
  assert.equal(layer.props.classes[64 * 4 + 3],255);
  assert.deepEqual(layer.props.cloudStyle.elevationRange,[120,160]);
  layer.props.onHover({picked:true,index:1,x:5,y:6});
  assert.equal(picked.index,1);
  assert.equal(picked.elevation,50);
  assert.deepEqual(picked.attributes,{returnNumber:2});
  // The mask also applies to a newly streamed batch containing class 2.
  manager.updatePointCloud('cloud',{...data,positions:data.positions.slice()});
  pending();
  assert.equal(layers.values().next().value.props.classes[2 * 4 + 3],0);
  manager.setColormap('viridis');
  manager.clear();
  assert.equal(pending,undefined);
  assert.equal(layers.size,0);
});
