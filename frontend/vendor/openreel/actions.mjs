/*! OpenReel MIT — Augustus Otu and Contributors. See upstream/LICENSE and PROVENANCE.md. */

// vendor/openreel/upstream/packages/core/src/motion/shaders/effect-shaders.ts
var DITHER_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_levels;
uniform float u_scale;
out vec4 fragColor;

const float bayer[16] = float[16](
  0.0, 8.0, 2.0, 10.0,
  12.0, 4.0, 14.0, 6.0,
  3.0, 11.0, 1.0, 9.0,
  15.0, 7.0, 13.0, 5.0
);

float bayerValue(vec2 pixel) {
  int x = int(mod(pixel.x, 4.0));
  int y = int(mod(pixel.y, 4.0));
  return bayer[y * 4 + x] / 16.0;
}

void main() {
  vec4 src = texture(u_input, vUv);
  float scale = max(u_scale, 1.0);
  float levels = max(u_levels, 2.0);
  vec2 pixel = floor((vUv * u_resolution) / scale);
  float threshold = bayerValue(pixel) - 0.5;
  vec3 scaled = src.rgb * (levels - 1.0);
  vec3 quantized = floor(scaled + 0.5 + threshold) / (levels - 1.0);
  fragColor = vec4(clamp(quantized, 0.0, 1.0), src.a);
}
`;
var GRADIENT_MAP_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_mix;
out vec4 fragColor;

const vec3 stopDark = vec3(0.05, 0.02, 0.18);
const vec3 stopLight = vec3(1.0, 0.86, 0.45);

void main() {
  vec4 src = texture(u_input, vUv);
  float luma = dot(src.rgb, vec3(0.2126, 0.7152, 0.0722));
  vec3 mapped = mix(stopDark, stopLight, luma);
  vec3 result = mix(src.rgb, mapped, clamp(u_mix, 0.0, 1.0));
  fragColor = vec4(result, src.a);
}
`;
var PIXELATE_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_size;
out vec4 fragColor;

void main() {
  float size = max(u_size, 1.0);
  vec2 blocks = max(u_resolution / size, vec2(1.0));
  vec2 quantized = (floor(vUv * blocks) + 0.5) / blocks;
  fragColor = texture(u_input, quantized);
}
`;
var HALFTONE_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_dotSize;
uniform float u_angle;
out vec4 fragColor;

void main() {
  vec4 src = texture(u_input, vUv);
  float luma = dot(src.rgb, vec3(0.2126, 0.7152, 0.0722));
  float dotSize = max(u_dotSize, 2.0);
  float rad = radians(u_angle);
  vec2 pixel = vUv * u_resolution;
  mat2 rot = mat2(cos(rad), -sin(rad), sin(rad), cos(rad));
  vec2 rotated = rot * pixel;
  vec2 cell = mod(rotated, dotSize) - dotSize * 0.5;
  float dist = length(cell) / (dotSize * 0.5);
  float radius = sqrt(1.0 - clamp(luma, 0.0, 1.0));
  float ink = step(dist, radius);
  vec3 result = mix(vec3(1.0), vec3(0.0), ink);
  fragColor = vec4(result, src.a);
}
`;
var VHS_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_intensity;
uniform float u_scanlines;
uniform float u_jitter;
out vec4 fragColor;

float hash(vec2 p) {
  return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

void main() {
  float intensity = clamp(u_intensity, 0.0, 1.0);
  float frame = floor(u_time * 24.0);
  float lineNoise = hash(vec2(frame, floor(vUv.y * 90.0)));
  float horizontalJitter = (lineNoise - 0.5) * u_jitter * 0.035;
  horizontalJitter *= step(0.82, lineNoise);
  vec2 uv = vec2(clamp(vUv.x + horizontalJitter, 0.0, 1.0), vUv.y);
  float split = (1.0 + u_jitter * 4.0) / max(u_resolution.x, 1.0);
  vec4 src = texture(u_input, uv);
  vec3 vhs = vec3(
    texture(u_input, vec2(clamp(uv.x + split, 0.0, 1.0), uv.y)).r,
    src.g,
    texture(u_input, vec2(clamp(uv.x - split, 0.0, 1.0), uv.y)).b
  );
  float scan = sin(vUv.y * u_resolution.y * 3.14159265);
  vhs *= 1.0 - (0.5 + 0.5 * scan) * clamp(u_scanlines, 0.0, 1.0) * 0.32;
  float grain = (hash(vUv * u_resolution + frame) - 0.5) * 0.11;
  vhs += grain * intensity;
  fragColor = vec4(clamp(mix(src.rgb, vhs, intensity), 0.0, 1.0), src.a);
}
`;
var POSTERIZE_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_levels;
uniform float u_mix;
out vec4 fragColor;

void main() {
  vec4 src = texture(u_input, vUv);
  float levels = max(2.0, floor(u_levels));
  vec3 posterized = floor(src.rgb * (levels - 1.0) + 0.5) / (levels - 1.0);
  fragColor = vec4(mix(src.rgb, posterized, clamp(u_mix, 0.0, 1.0)), src.a);
}
`;
var DUOTONE_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_time;
uniform vec4 u_shadowColor;
uniform vec4 u_highlightColor;
uniform float u_mix;
uniform float u_contrast;
out vec4 fragColor;

void main() {
  vec4 src = texture(u_input, vUv);
  float luma = dot(src.rgb, vec3(0.2126, 0.7152, 0.0722));
  luma = clamp((luma - 0.5) * max(u_contrast, 0.1) + 0.5, 0.0, 1.0);
  vec3 mapped = mix(u_shadowColor.rgb, u_highlightColor.rgb, luma);
  fragColor = vec4(mix(src.rgb, mapped, clamp(u_mix, 0.0, 1.0)), src.a);
}
`;
var PRISM_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_amount;
uniform float u_angle;
uniform float u_mix;
out vec4 fragColor;

void main() {
  vec2 texel = 1.0 / max(u_resolution, vec2(1.0));
  float rad = radians(u_angle);
  vec2 direction = vec2(cos(rad), sin(rad));
  vec2 offset = direction * texel * u_amount;
  vec4 src = texture(u_input, vUv);
  vec3 prism = vec3(
    texture(u_input, clamp(vUv + offset, vec2(0.0), vec2(1.0))).r,
    src.g,
    texture(u_input, clamp(vUv - offset, vec2(0.0), vec2(1.0))).b
  );
  fragColor = vec4(mix(src.rgb, prism, clamp(u_mix, 0.0, 1.0)), src.a);
}
`;
var FISHEYE_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_strength;
uniform float u_radius;
out vec4 fragColor;

void main() {
  vec2 aspect = vec2(u_resolution.x / max(u_resolution.y, 1.0), 1.0);
  vec2 centered = (vUv - 0.5) * aspect;
  float distanceFromCenter = length(centered);
  float radius = max(u_radius, 0.1);
  float falloff = 1.0 - smoothstep(radius * 0.75, radius, distanceFromCenter);
  float distortion = 1.0 + u_strength * dot(centered, centered) * falloff;
  vec2 distorted = centered * distortion;
  vec2 uv = clamp(distorted / aspect + 0.5, vec2(0.0), vec2(1.0));
  vec4 src = texture(u_input, vUv);
  vec4 warped = texture(u_input, uv);
  fragColor = vec4(warped.rgb, src.a);
}
`;
var WAVE_WARP_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_amplitude;
uniform float u_frequency;
uniform float u_speed;
out vec4 fragColor;

void main() {
  float phase = vUv.y * u_frequency * 6.2831853 + u_time * u_speed;
  vec2 uv = vec2(clamp(vUv.x + sin(phase) * u_amplitude, 0.0, 1.0), vUv.y);
  vec4 warped = texture(u_input, uv);
  vec4 src = texture(u_input, vUv);
  fragColor = vec4(warped.rgb, src.a);
}
`;
var SCANLINES_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_density;
uniform float u_intensity;
uniform float u_speed;
out vec4 fragColor;

void main() {
  vec4 src = texture(u_input, vUv);
  float position = vUv.y * u_density + u_time * u_speed * 20.0;
  float line = 0.5 + 0.5 * sin(position * 3.14159265);
  float shade = 1.0 - line * clamp(u_intensity, 0.0, 1.0);
  fragColor = vec4(src.rgb * shade, src.a);
}
`;
var EDGE_GLOW_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_strength;
uniform float u_radius;
uniform vec4 u_color;
out vec4 fragColor;

float lumaAt(vec2 uv) {
  return dot(texture(u_input, clamp(uv, vec2(0.0), vec2(1.0))).rgb, vec3(0.2126, 0.7152, 0.0722));
}

void main() {
  vec4 src = texture(u_input, vUv);
  vec2 texel = max(u_radius, 0.5) / max(u_resolution, vec2(1.0));
  float gx = lumaAt(vUv + vec2(texel.x, 0.0)) - lumaAt(vUv - vec2(texel.x, 0.0));
  float gy = lumaAt(vUv + vec2(0.0, texel.y)) - lumaAt(vUv - vec2(0.0, texel.y));
  float edge = clamp(length(vec2(gx, gy)) * u_strength, 0.0, 1.0);
  vec3 result = src.rgb + u_color.rgb * edge * u_color.a;
  fragColor = vec4(clamp(result, 0.0, 1.0), src.a);
}
`;
var EFFECT_SHADERS = [
  {
    id: "dither",
    name: "Dither",
    category: "effect",
    glsl: DITHER_GLSL,
    params: [
      { name: "levels", label: "Levels", type: "number", default: 4, min: 2, max: 16, step: 1 },
      { name: "scale", label: "Scale", type: "number", default: 1, min: 1, max: 8, step: 1 }
    ]
  },
  {
    id: "gradient-map",
    name: "Gradient Map",
    category: "effect",
    glsl: GRADIENT_MAP_GLSL,
    params: [
      { name: "mix", label: "Mix", type: "number", default: 1, min: 0, max: 1, step: 0.01 }
    ]
  },
  {
    id: "pixelate",
    name: "Pixelate",
    category: "effect",
    glsl: PIXELATE_GLSL,
    params: [
      { name: "size", label: "Size", type: "number", default: 8, min: 1, max: 64, step: 1 }
    ]
  },
  {
    id: "halftone",
    name: "Halftone",
    category: "effect",
    glsl: HALFTONE_GLSL,
    params: [
      { name: "dotSize", label: "Dot Size", type: "number", default: 8, min: 2, max: 32, step: 1 },
      { name: "angle", label: "Angle", type: "number", default: 15, min: 0, max: 90, step: 1 }
    ]
  },
  {
    id: "vhs",
    name: "VHS",
    category: "effect",
    glsl: VHS_GLSL,
    params: [
      { name: "intensity", label: "Intensity", type: "number", default: 0.75, min: 0, max: 1, step: 0.01 },
      { name: "scanlines", label: "Scanlines", type: "number", default: 0.4, min: 0, max: 1, step: 0.01 },
      { name: "jitter", label: "Jitter", type: "number", default: 0.45, min: 0, max: 1, step: 0.01 }
    ]
  },
  {
    id: "posterize",
    name: "Posterize",
    category: "effect",
    glsl: POSTERIZE_GLSL,
    params: [
      { name: "levels", label: "Levels", type: "number", default: 5, min: 2, max: 16, step: 1 },
      { name: "mix", label: "Mix", type: "number", default: 1, min: 0, max: 1, step: 0.01 }
    ]
  },
  {
    id: "duotone",
    name: "Duotone",
    category: "effect",
    glsl: DUOTONE_GLSL,
    params: [
      { name: "shadowColor", label: "Shadow", type: "color", default: "#11133f", min: 0, max: 1, step: 0.01 },
      { name: "highlightColor", label: "Highlight", type: "color", default: "#ffca6b", min: 0, max: 1, step: 0.01 },
      { name: "mix", label: "Mix", type: "number", default: 0.9, min: 0, max: 1, step: 0.01 },
      { name: "contrast", label: "Contrast", type: "number", default: 1.15, min: 0.25, max: 2.5, step: 0.05 }
    ]
  },
  {
    id: "prism",
    name: "Prism Split",
    category: "effect",
    glsl: PRISM_GLSL,
    params: [
      { name: "amount", label: "Offset", type: "number", default: 8, min: 0, max: 40, step: 0.5 },
      { name: "angle", label: "Angle", type: "number", default: 0, min: 0, max: 360, step: 1 },
      { name: "mix", label: "Mix", type: "number", default: 1, min: 0, max: 1, step: 0.01 }
    ]
  },
  {
    id: "fisheye",
    name: "Fisheye",
    category: "effect",
    glsl: FISHEYE_GLSL,
    params: [
      { name: "strength", label: "Strength", type: "number", default: 0.55, min: -1, max: 1.5, step: 0.05 },
      { name: "radius", label: "Radius", type: "number", default: 0.8, min: 0.2, max: 1.5, step: 0.05 }
    ]
  },
  {
    id: "wave-warp",
    name: "Wave Warp",
    category: "effect",
    glsl: WAVE_WARP_GLSL,
    params: [
      { name: "amplitude", label: "Amplitude", type: "number", default: 0.025, min: 0, max: 0.15, step: 5e-3 },
      { name: "frequency", label: "Frequency", type: "number", default: 5, min: 1, max: 20, step: 0.5 },
      { name: "speed", label: "Speed", type: "number", default: 1.5, min: 0, max: 8, step: 0.1 }
    ]
  },
  {
    id: "scanlines",
    name: "Scanlines",
    category: "effect",
    glsl: SCANLINES_GLSL,
    params: [
      { name: "density", label: "Density", type: "number", default: 360, min: 40, max: 1200, step: 10 },
      { name: "intensity", label: "Intensity", type: "number", default: 0.3, min: 0, max: 1, step: 0.01 },
      { name: "speed", label: "Speed", type: "number", default: 0.2, min: 0, max: 4, step: 0.05 }
    ]
  },
  {
    id: "edge-glow",
    name: "Edge Glow",
    category: "effect",
    glsl: EDGE_GLOW_GLSL,
    params: [
      { name: "strength", label: "Strength", type: "number", default: 4, min: 0, max: 12, step: 0.25 },
      { name: "radius", label: "Radius", type: "number", default: 1.5, min: 0.5, max: 6, step: 0.25 },
      { name: "color", label: "Glow Color", type: "color", default: "#4de8ff", min: 0, max: 1, step: 0.01 }
    ]
  }
];

// vendor/openreel/upstream/packages/core/src/motion/shaders/fill-shaders.ts
var LIQUID_METAL_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_scale;
uniform float u_speed;
uniform float u_contrast;
out vec4 fragColor;

float hash(vec2 p) {
  p = fract(p * vec2(123.34, 456.21));
  p += dot(p, p + 45.32);
  return fract(p.x * p.y);
}

float valueNoise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  float a = hash(i);
  float b = hash(i + vec2(1.0, 0.0));
  float c = hash(i + vec2(0.0, 1.0));
  float d = hash(i + vec2(1.0, 1.0));
  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

float fbm(vec2 p) {
  float sum = 0.0;
  float amp = 0.5;
  for (int i = 0; i < 5; i++) {
    sum += amp * valueNoise(p);
    p *= 2.0;
    amp *= 0.5;
  }
  return sum;
}

void main() {
  float scale = max(u_scale, 1.0);
  vec2 p = vUv * scale;
  float flow = u_time * u_speed;
  float field = fbm(p + vec2(flow, flow * 0.5));
  field += 0.4 * sin((p.x + p.y) * 1.5 + field * 6.2831 + flow);
  float ramp = 0.5 + 0.5 * sin(field * 6.2831 * u_contrast);
  float metal = pow(clamp(ramp, 0.0, 1.0), 1.6);
  vec3 color = mix(vec3(0.08, 0.09, 0.11), vec3(0.92, 0.94, 0.98), metal);
  fragColor = vec4(color, 1.0);
}
`;
var WATERCOLOR_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_scale;
uniform float u_bleed;
out vec4 fragColor;

float hash(vec2 p) {
  p = fract(p * vec2(91.73, 53.41));
  p += dot(p, p + 21.97);
  return fract(p.x * p.y);
}

float valueNoise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  float a = hash(i);
  float b = hash(i + vec2(1.0, 0.0));
  float c = hash(i + vec2(0.0, 1.0));
  float d = hash(i + vec2(1.0, 1.0));
  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

void main() {
  float scale = max(u_scale, 1.0);
  vec2 p = vUv * scale;
  float bleed = clamp(u_bleed, 0.0, 1.0);
  float base = valueNoise(p);
  float mid = valueNoise(p * 2.0 + 7.3);
  float fine = valueNoise(p * 4.0 + 19.1);
  float mottle = base * 0.55 + mid * 0.3 + fine * 0.15;
  float soft = mix(mottle, smoothstep(0.2, 0.8, mottle), bleed);
  vec3 paper = mix(vec3(0.97, 0.96, 0.92), vec3(0.74, 0.78, 0.86), soft);
  fragColor = vec4(paper, 1.0);
}
`;
var GRADIENT_NOISE_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_scale;
uniform float u_warp;
out vec4 fragColor;

float hash(vec2 p) {
  p = fract(p * vec2(127.1, 311.7));
  p += dot(p, p + 34.45);
  return fract(p.x * p.y);
}

float valueNoise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  float a = hash(i);
  float b = hash(i + vec2(1.0, 0.0));
  float c = hash(i + vec2(0.0, 1.0));
  float d = hash(i + vec2(1.0, 1.0));
  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

void main() {
  float scale = max(u_scale, 1.0);
  float warp = clamp(u_warp, 0.0, 1.0);
  vec2 p = vUv * scale;
  vec2 offset = vec2(valueNoise(p + 1.7), valueNoise(p + 9.2));
  vec2 warped = p + warp * (offset - 0.5) * 4.0;
  float field = valueNoise(warped);
  vec3 lo = vec3(0.12, 0.20, 0.45);
  vec3 hi = vec3(0.95, 0.55, 0.30);
  vec3 color = mix(lo, hi, clamp(field, 0.0, 1.0));
  fragColor = vec4(color, 1.0);
}
`;
var FILL_SHADERS = [
  {
    id: "liquid-metal",
    name: "Liquid Metal",
    category: "fill",
    glsl: LIQUID_METAL_GLSL,
    params: [
      { name: "scale", label: "Scale", type: "number", default: 6, min: 1, max: 20, step: 0.5, control: "number" },
      { name: "speed", label: "Speed", type: "number", default: 0, min: 0, max: 2, step: 0.05, control: "number" },
      { name: "contrast", label: "Contrast", type: "number", default: 1.4, min: 0.5, max: 3, step: 0.1, control: "number" }
    ]
  },
  {
    id: "watercolor",
    name: "Watercolor",
    category: "fill",
    glsl: WATERCOLOR_GLSL,
    params: [
      { name: "scale", label: "Scale", type: "number", default: 5, min: 1, max: 16, step: 0.5, control: "number" },
      { name: "bleed", label: "Bleed", type: "number", default: 0.5, min: 0, max: 1, step: 0.05, control: "slider" }
    ]
  },
  {
    id: "gradient-noise",
    name: "Gradient Noise",
    category: "fill",
    glsl: GRADIENT_NOISE_GLSL,
    params: [
      { name: "scale", label: "Scale", type: "number", default: 8, min: 1, max: 24, step: 0.5, control: "number" },
      { name: "warp", label: "Warp", type: "number", default: 0.4, min: 0, max: 1, step: 0.05, control: "slider" }
    ]
  }
];

// vendor/openreel/upstream/packages/core/src/motion/shaders/text-shaders.ts
var GLYPH_DISSOLVE_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D u_input;
uniform float u_time;
uniform float u_progress;
uniform float u_edgeWidth;
uniform float u_scale;
out vec4 fragColor;
float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float vnoise(vec2 p){
  vec2 i = floor(p); vec2 f = fract(p);
  vec2 u = f*f*(3.0-2.0*f);
  return mix(mix(hash(i), hash(i+vec2(1,0)), u.x),
             mix(hash(i+vec2(0,1)), hash(i+vec2(1,1)), u.x), u.y);
}
void main(){
  vec4 src = texture(u_input, vUv);
  float n = vnoise(vUv * u_scale + u_time * 0.05);
  float edge = max(u_edgeWidth, 0.001);
  float reveal = smoothstep(n - edge, n + edge, u_progress);
  fragColor = vec4(src.rgb, src.a * reveal);
}
`;
var GLYPH_GLOW_WAVE_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_progress;
uniform float u_glow;
uniform float u_softness;
out vec4 fragColor;
void main(){
  vec4 src = texture(u_input, vUv);
  float softness = clamp(u_softness, 0.0, 1.0);
  vec2 texel = 1.0 / max(u_resolution, vec2(1.0));
  float radius = 1.0 + softness * 3.0;
  float coverage = 0.0;
  coverage += texture(u_input, vUv + texel * vec2(radius, 0.0)).a;
  coverage += texture(u_input, vUv - texel * vec2(radius, 0.0)).a;
  coverage += texture(u_input, vUv + texel * vec2(0.0, radius)).a;
  coverage += texture(u_input, vUv - texel * vec2(0.0, radius)).a;
  coverage += texture(u_input, vUv + texel * radius).a;
  coverage += texture(u_input, vUv - texel * radius).a;
  coverage *= 0.16666667;
  float peak = sin(clamp(u_progress, 0.0, 1.0) * 3.14159265);
  float glow = max(u_glow, 0.0) * peak;
  vec3 lit = src.rgb + src.rgb * glow;
  float halo = coverage * glow;
  vec3 color = lit + vec3(halo);
  float alpha = clamp(src.a + halo, 0.0, 1.0);
  fragColor = vec4(color, alpha);
}
`;
var CHROMATIC_CASCADE_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D u_input;
uniform float u_progress;
uniform float u_amount;
out vec4 fragColor;
void main(){
  float p = clamp(u_progress, 0.0, 1.0);
  float split = max(u_amount, 0.0) * (1.0 - p);
  vec2 shift = vec2(split, 0.0);
  float r = texture(u_input, vUv + shift).r;
  vec4 g = texture(u_input, vUv);
  float b = texture(u_input, vUv - shift).b;
  float alpha = max(g.a, max(texture(u_input, vUv + shift).a, texture(u_input, vUv - shift).a));
  fragColor = vec4(r, g.g, b, alpha);
}
`;
var SCANLINE_MATERIALIZE_GLSL = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_progress;
uniform float u_lines;
uniform float u_jitter;
out vec4 fragColor;
float hash(float x){ return fract(sin(x * 78.233) * 43758.5453); }
void main(){
  vec4 src = texture(u_input, vUv);
  float p = clamp(u_progress, 0.0, 1.0);
  float lines = max(u_lines, 1.0);
  float jitter = clamp(u_jitter, 0.0, 1.0);
  float row = floor(vUv.y * lines);
  float flicker = (hash(row + floor(u_time * 30.0)) - 0.5) * jitter;
  float fade = smoothstep(0.0, 0.15, p) * smoothstep(0.0, 0.15, 1.0 - p);
  float thr = clamp(p + flicker * fade, 0.0, 1.0);
  float reveal = step(vUv.y, thr);
  fragColor = vec4(src.rgb, src.a * reveal);
}
`;
var TEXT_SHADERS = [
  {
    id: "glyph-dissolve",
    name: "Glyph Dissolve",
    category: "text",
    glsl: GLYPH_DISSOLVE_GLSL,
    params: [
      { name: "edgeWidth", label: "Edge Width", type: "number", default: 0.15, min: 0, max: 1, step: 0.01, control: "slider" },
      { name: "scale", label: "Scale", type: "number", default: 12, min: 2, max: 40, step: 1, control: "number" }
    ]
  },
  {
    id: "glyph-glow-wave",
    name: "Glyph Glow Wave",
    category: "text",
    glsl: GLYPH_GLOW_WAVE_GLSL,
    params: [
      { name: "glow", label: "Glow", type: "number", default: 1.4, min: 0, max: 3, step: 0.1, control: "number" },
      { name: "softness", label: "Softness", type: "number", default: 0.5, min: 0, max: 1, step: 0.05, control: "slider" }
    ]
  },
  {
    id: "chromatic-cascade",
    name: "Chromatic Cascade",
    category: "text",
    glsl: CHROMATIC_CASCADE_GLSL,
    params: [
      { name: "amount", label: "Amount", type: "number", default: 0.03, min: 0, max: 0.1, step: 5e-3, control: "slider" }
    ]
  },
  {
    id: "scanline-materialize",
    name: "Scanline Materialize",
    category: "text",
    glsl: SCANLINE_MATERIALIZE_GLSL,
    params: [
      { name: "lines", label: "Lines", type: "number", default: 80, min: 10, max: 200, step: 5, control: "number" },
      { name: "jitter", label: "Jitter", type: "number", default: 0.3, min: 0, max: 1, step: 0.05, control: "slider" }
    ]
  }
];

// vendor/openreel/upstream/packages/core/src/motion/shaders/registry.ts
var BUILTIN_SHADER_IDS = new Set(
  [...EFFECT_SHADERS, ...FILL_SHADERS, ...TEXT_SHADERS].map((def) => def.id)
);
var generatedShaders = /* @__PURE__ */ new Map();
function registerMotionShader(def) {
  if (BUILTIN_SHADER_IDS.has(def.id)) {
    throw new Error("Cannot overwrite built-in shader: " + def.id);
  }
  generatedShaders.set(def.id, def);
}
function unregisterMotionShader(id) {
  generatedShaders.delete(id);
}
function isBuiltinMotionShaderId(id) {
  return BUILTIN_SHADER_IDS.has(id);
}

// vendor/openreel/upstream/packages/core/src/types/timeline.ts
var EASING_TYPES = [
  "linear",
  "ease",
  "ease-in",
  "ease-out",
  "ease-in-out",
  "hold",
  "bezier",
  "smoothstep",
  "smootherstep",
  "snappy",
  "smooth",
  "easeInQuad",
  "easeOutQuad",
  "easeInOutQuad",
  "easeInCubic",
  "easeOutCubic",
  "easeInOutCubic",
  "easeInQuart",
  "easeOutQuart",
  "easeInOutQuart",
  "easeInQuint",
  "easeOutQuint",
  "easeInOutQuint",
  "easeInSine",
  "easeOutSine",
  "easeInOutSine",
  "easeInExpo",
  "easeOutExpo",
  "easeInOutExpo",
  "easeInCirc",
  "easeOutCirc",
  "easeInOutCirc",
  "easeInBack",
  "easeOutBack",
  "easeInOutBack",
  "easeInBackStrong",
  "easeOutBackStrong",
  "easeInOutBackStrong",
  "easeInElastic",
  "easeOutElastic",
  "easeInOutElastic",
  "easeInElasticSoft",
  "easeOutElasticSoft",
  "easeInOutElasticSoft",
  "easeInBounce",
  "easeOutBounce",
  "easeInOutBounce",
  "easeInBounceSmall",
  "easeOutBounceSmall",
  "easeInOutBounceSmall"
];

// vendor/openreel/upstream/packages/core/src/types/effects.ts
var TRANSITION_TYPES = [
  "crossfade",
  "dipToBlack",
  "dipToWhite",
  "wipe",
  "slide",
  "zoom",
  "push",
  "circleReveal",
  "blur",
  "whipPan",
  "radialWipe",
  "pixelate",
  "glitch",
  "blinds",
  "diamondReveal",
  "spin",
  "flip",
  "splitReveal",
  "flash",
  "filmBurn",
  "mosaic",
  "ripple",
  "pageTurn",
  "colorSplit"
];

// vendor/openreel/upstream/packages/core/src/actions/registry.ts
var registry = /* @__PURE__ */ new Map();
function registerActionHandler(handler) {
  registry.set(handler.type, handler);
}
function getActionHandler(type) {
  return registry.get(type);
}

// vendor/openreel/upstream/packages/core/src/actions/action-validator.ts
var ActionValidator = class {
  validate(action, project) {
    const errors = [];
    if (!action.type || typeof action.type !== "string") {
      errors.push({
        code: "INVALID_TYPE",
        message: "Action type is required and must be a string"
      });
      return { valid: false, errors };
    }
    if (!action.params || typeof action.params !== "object") {
      errors.push({
        code: "INVALID_PARAMS",
        message: "Action params are required and must be an object"
      });
      return { valid: false, errors };
    }
    const handler = getActionHandler(action.type);
    if (handler) {
      return handler.validate(action, project);
    }
    const typeValidationErrors = this.validateActionType(
      action,
      project
    );
    errors.push(...typeValidationErrors);
    return {
      valid: errors.length === 0,
      errors
    };
  }
  validateActionType(action, project) {
    const type = action.type;
    if (type.startsWith("project/")) {
      return this.validateProjectAction(action, project);
    } else if (type.startsWith("media/")) {
      return this.validateMediaAction(action, project);
    } else if (type.startsWith("track/")) {
      return this.validateTrackAction(action, project);
    } else if (type.startsWith("clip/")) {
      return this.validateClipAction(action, project);
    } else if (type.startsWith("effect/")) {
      return this.validateEffectAction(action, project);
    } else if (type.startsWith("transform/")) {
      return this.validateTransformAction(action, project);
    } else if (type.startsWith("keyframe/")) {
      return this.validateKeyframeAction(action, project);
    } else if (type.startsWith("transition/")) {
      return this.validateTransitionAction(action, project);
    } else if (type.startsWith("audio/")) {
      return this.validateAudioAction(action, project);
    } else if (type.startsWith("subtitle/")) {
      return this.validateSubtitleAction(action, project);
    } else if (type.startsWith("marker/")) {
      return this.validateMarkerAction(action, project);
    }
    return [
      {
        code: "UNKNOWN_ACTION_TYPE",
        message: `Unknown action type: ${type}`
      }
    ];
  }
  validateProjectAction(action, _project) {
    const errors = [];
    switch (action.type) {
      case "project/create":
        if (!action.params.name || typeof action.params.name !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Project name is required and must be a string",
            path: "params.name"
          });
        }
        if (!action.params.settings) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Project settings are required",
            path: "params.settings"
          });
        }
        break;
      case "project/rename":
        if (!action.params.name || typeof action.params.name !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Project name is required and must be a string",
            path: "params.name"
          });
        }
        break;
      case "project/updateSettings":
      case "project/setCanvasBackground":
        if (!action.params || typeof action.params !== "object" || Array.isArray(action.params)) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Params must be an object",
            path: "params"
          });
        }
        break;
      case "project/registerGeneratedShader":
        if (!action.params.def || typeof action.params.def !== "object" || typeof action.params.def.id !== "string" || typeof action.params.def.glsl !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "A valid shader def is required",
            path: "params.def"
          });
        }
        break;
      case "project/removeGeneratedShader":
        if (!action.params.shaderId || typeof action.params.shaderId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "shaderId is required and must be a string",
            path: "params.shaderId"
          });
        }
        break;
    }
    return errors;
  }
  validateMediaAction(action, project) {
    const errors = [];
    switch (action.type) {
      case "media/import":
        if (!action.params.file) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "File is required for media import",
            path: "params.file"
          });
        }
        break;
      case "media/delete":
      case "media/rename":
        if (!action.params.mediaId || typeof action.params.mediaId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Media ID is required and must be a string",
            path: "params.mediaId"
          });
        } else {
          const mediaExists = project.mediaLibrary.items.some(
            (item) => item.id === action.params.mediaId
          );
          if (!mediaExists) {
            errors.push({
              code: "MEDIA_NOT_FOUND",
              message: `Media with ID ${action.params.mediaId} not found`,
              path: "params.mediaId"
            });
          }
        }
        if (action.type === "media/rename") {
          if (!action.params.name || typeof action.params.name !== "string") {
            errors.push({
              code: "INVALID_PARAMS",
              message: "Media name is required and must be a string",
              path: "params.name"
            });
          }
        }
        break;
    }
    return errors;
  }
  validateTrackAction(action, project) {
    const errors = [];
    const timeline = project.timeline;
    switch (action.type) {
      case "track/add":
        if (!["video", "audio", "image", "text", "graphics"].includes(
          action.params.trackType
        )) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Track type must be 'video', 'audio', 'image', 'text', or 'graphics'",
            path: "params.trackType"
          });
        }
        if (action.params.position !== void 0 && (typeof action.params.position !== "number" || action.params.position < 0)) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Track position must be a non-negative number",
            path: "params.position"
          });
        }
        break;
      case "track/duplicate":
        if (!action.params.sourceTrackId || typeof action.params.sourceTrackId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Source track ID is required and must be a string",
            path: "params.sourceTrackId"
          });
        } else if (!this.findTrack(timeline, action.params.sourceTrackId)) {
          errors.push({
            code: "TRACK_NOT_FOUND",
            message: `Track with ID ${action.params.sourceTrackId} not found`,
            path: "params.sourceTrackId"
          });
        }
        if (action.params.position !== void 0 && (typeof action.params.position !== "number" || action.params.position < 0 || action.params.position > timeline.tracks.length)) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Track position must be within the timeline",
            path: "params.position"
          });
        }
        break;
      case "track/remove":
      case "track/lock":
      case "track/hide":
      case "track/mute":
      case "track/solo":
        if (!action.params.trackId || typeof action.params.trackId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Track ID is required and must be a string",
            path: "params.trackId"
          });
        } else {
          const track = this.findTrack(timeline, action.params.trackId);
          if (!track) {
            errors.push({
              code: "TRACK_NOT_FOUND",
              message: `Track with ID ${action.params.trackId} not found`,
              path: "params.trackId"
            });
          }
        }
        if (action.type === "track/lock" && typeof action.params.locked !== "boolean") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Locked parameter must be a boolean",
            path: "params.locked"
          });
        }
        if (action.type === "track/hide" && typeof action.params.hidden !== "boolean") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Hidden parameter must be a boolean",
            path: "params.hidden"
          });
        }
        if (action.type === "track/mute" && typeof action.params.muted !== "boolean") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Muted parameter must be a boolean",
            path: "params.muted"
          });
        }
        if (action.type === "track/solo" && typeof action.params.solo !== "boolean") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Solo parameter must be a boolean",
            path: "params.solo"
          });
        }
        break;
      case "track/reorder":
        if (!action.params.trackId || typeof action.params.trackId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Track ID is required and must be a string",
            path: "params.trackId"
          });
        } else {
          const track = this.findTrack(timeline, action.params.trackId);
          if (!track) {
            errors.push({
              code: "TRACK_NOT_FOUND",
              message: `Track with ID ${action.params.trackId} not found`,
              path: "params.trackId"
            });
          }
        }
        if (typeof action.params.newPosition !== "number" || action.params.newPosition < 0 || action.params.newPosition >= timeline.tracks.length) {
          errors.push({
            code: "INVALID_PARAMS",
            message: `New position must be between 0 and ${timeline.tracks.length - 1}`,
            path: "params.newPosition"
          });
        }
        break;
      case "track/rename":
        if (!action.params.trackId || typeof action.params.trackId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Track ID is required and must be a string",
            path: "params.trackId"
          });
        } else {
          const track = this.findTrack(timeline, action.params.trackId);
          if (!track) {
            errors.push({
              code: "TRACK_NOT_FOUND",
              message: `Track with ID ${action.params.trackId} not found`,
              path: "params.trackId"
            });
          }
        }
        if (typeof action.params.name !== "string" || action.params.name.trim().length === 0) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Track name is required and must be a non-empty string",
            path: "params.name"
          });
        }
        break;
    }
    return errors;
  }
  validateMarkerAction(action, project) {
    const errors = [];
    const timeline = project.timeline;
    switch (action.type) {
      case "marker/add":
        if (typeof action.params.time !== "number" || action.params.time < 0) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Marker time must be a non-negative number",
            path: "params.time"
          });
        }
        break;
      case "marker/remove":
      case "marker/update":
        if (!action.params.markerId || typeof action.params.markerId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Marker ID is required and must be a string",
            path: "params.markerId"
          });
        } else {
          const marker = timeline.markers.find(
            (m) => m.id === action.params.markerId
          );
          if (!marker) {
            errors.push({
              code: "INVALID_PARAMS",
              message: `Marker with ID ${action.params.markerId} not found`,
              path: "params.markerId"
            });
          }
        }
        break;
    }
    return errors;
  }
  validateClipAction(action, project) {
    const errors = [];
    const timeline = project.timeline;
    switch (action.type) {
      case "clip/add":
        if (!action.params.trackId || typeof action.params.trackId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Track ID is required and must be a string",
            path: "params.trackId"
          });
        } else {
          const track = this.findTrack(timeline, action.params.trackId);
          if (!track) {
            errors.push({
              code: "TRACK_NOT_FOUND",
              message: `Track with ID ${action.params.trackId} not found`,
              path: "params.trackId"
            });
          } else if (track.locked) {
            errors.push({
              code: "TRACK_LOCKED",
              message: `Track ${action.params.trackId} is locked`,
              path: "params.trackId"
            });
          }
        }
        if (!action.params.mediaId || typeof action.params.mediaId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Media ID is required and must be a string",
            path: "params.mediaId"
          });
        } else {
          const mediaExists = project.mediaLibrary.items.some(
            (item) => item.id === action.params.mediaId
          );
          if (!mediaExists) {
            errors.push({
              code: "MEDIA_NOT_FOUND",
              message: `Media with ID ${action.params.mediaId} not found`,
              path: "params.mediaId"
            });
          }
        }
        if (typeof action.params.startTime !== "number" || action.params.startTime < 0) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Start time must be a non-negative number",
            path: "params.startTime"
          });
        }
        break;
      case "clip/remove":
      case "clip/rippleDelete":
        if (!action.params.clipId || typeof action.params.clipId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Clip ID is required and must be a string",
            path: "params.clipId"
          });
        } else {
          const clip = this.findClip(timeline, action.params.clipId);
          if (!clip) {
            errors.push({
              code: "CLIP_NOT_FOUND",
              message: `Clip with ID ${action.params.clipId} not found`,
              path: "params.clipId"
            });
          } else {
            const track = this.findTrack(timeline, clip.trackId);
            if (track?.locked) {
              errors.push({
                code: "TRACK_LOCKED",
                message: `Track containing clip ${action.params.clipId} is locked`,
                path: "params.clipId"
              });
            }
          }
        }
        break;
      case "clip/move":
        if (!action.params.clipId || typeof action.params.clipId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Clip ID is required and must be a string",
            path: "params.clipId"
          });
        } else {
          const clip = this.findClip(timeline, action.params.clipId);
          if (!clip) {
            errors.push({
              code: "CLIP_NOT_FOUND",
              message: `Clip with ID ${action.params.clipId} not found`,
              path: "params.clipId"
            });
          } else {
            const track = this.findTrack(timeline, clip.trackId);
            if (track?.locked) {
              errors.push({
                code: "TRACK_LOCKED",
                message: `Track containing clip ${action.params.clipId} is locked`,
                path: "params.clipId"
              });
            }
          }
        }
        if (typeof action.params.startTime !== "number" || action.params.startTime < 0) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Start time must be a non-negative number",
            path: "params.startTime"
          });
        }
        if (action.params.trackId !== void 0) {
          if (typeof action.params.trackId !== "string") {
            errors.push({
              code: "INVALID_PARAMS",
              message: "Track ID must be a string",
              path: "params.trackId"
            });
          } else {
            const targetTrack = this.findTrack(timeline, action.params.trackId);
            if (!targetTrack) {
              errors.push({
                code: "TRACK_NOT_FOUND",
                message: `Target track with ID ${action.params.trackId} not found`,
                path: "params.trackId"
              });
            } else if (targetTrack.locked) {
              errors.push({
                code: "TRACK_LOCKED",
                message: `Target track ${action.params.trackId} is locked`,
                path: "params.trackId"
              });
            }
          }
        }
        break;
      case "clip/trim":
        if (!action.params.clipId || typeof action.params.clipId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Clip ID is required and must be a string",
            path: "params.clipId"
          });
        } else {
          const clip = this.findClip(timeline, action.params.clipId);
          if (!clip) {
            errors.push({
              code: "CLIP_NOT_FOUND",
              message: `Clip with ID ${action.params.clipId} not found`,
              path: "params.clipId"
            });
          } else {
            const track = this.findTrack(timeline, clip.trackId);
            if (track?.locked) {
              errors.push({
                code: "TRACK_LOCKED",
                message: `Track containing clip ${action.params.clipId} is locked`,
                path: "params.clipId"
              });
            }
          }
        }
        if (action.params.inPoint !== void 0 && (typeof action.params.inPoint !== "number" || action.params.inPoint < 0)) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "In-point must be a non-negative number",
            path: "params.inPoint"
          });
        }
        if (action.params.outPoint !== void 0 && (typeof action.params.outPoint !== "number" || action.params.outPoint < 0)) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Out-point must be a non-negative number",
            path: "params.outPoint"
          });
        }
        if (action.params.inPoint !== void 0 && action.params.outPoint !== void 0 && action.params.outPoint <= action.params.inPoint) {
          errors.push({
            code: "INVALID_TIME_RANGE",
            message: "Out-point must be greater than in-point",
            path: "params"
          });
        }
        break;
      case "clip/split":
        if (!action.params.clipId || typeof action.params.clipId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Clip ID is required and must be a string",
            path: "params.clipId"
          });
        } else {
          const clip = this.findClip(timeline, action.params.clipId);
          if (!clip) {
            errors.push({
              code: "CLIP_NOT_FOUND",
              message: `Clip with ID ${action.params.clipId} not found`,
              path: "params.clipId"
            });
          } else {
            const track = this.findTrack(timeline, clip.trackId);
            if (track?.locked) {
              errors.push({
                code: "TRACK_LOCKED",
                message: `Track containing clip ${action.params.clipId} is locked`,
                path: "params.clipId"
              });
            }
            if (typeof action.params.time === "number") {
              if (action.params.time <= clip.startTime || action.params.time >= clip.startTime + clip.duration) {
                errors.push({
                  code: "OUT_OF_BOUNDS",
                  message: `Split time must be within clip bounds (${clip.startTime} to ${clip.startTime + clip.duration})`,
                  path: "params.time"
                });
              }
            } else {
              errors.push({
                code: "INVALID_PARAMS",
                message: "Split time is required and must be a number",
                path: "params.time"
              });
            }
          }
        }
        break;
      case "clip/setBlendMode":
      case "clip/setBlendOpacity":
      case "clip/setEmphasisAnimation":
      case "clip/setColorGrading":
        if (!action.params.clipId || typeof action.params.clipId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Clip ID is required and must be a string",
            path: "params.clipId"
          });
        } else {
          const clip = this.findClip(timeline, action.params.clipId);
          if (!clip) {
            errors.push({
              code: "CLIP_NOT_FOUND",
              message: `Clip with ID ${action.params.clipId} not found`,
              path: "params.clipId"
            });
          } else {
            const track = this.findTrack(timeline, clip.trackId);
            if (track?.locked) {
              errors.push({
                code: "TRACK_LOCKED",
                message: `Track containing clip ${action.params.clipId} is locked`,
                path: "params.clipId"
              });
            }
          }
        }
        break;
    }
    return errors;
  }
  validateEffectAction(action, project) {
    const errors = [];
    const timeline = project.timeline;
    if (!action.params.clipId || typeof action.params.clipId !== "string") {
      errors.push({
        code: "INVALID_PARAMS",
        message: "Clip ID is required and must be a string",
        path: "params.clipId"
      });
      return errors;
    }
    const clip = this.findClip(timeline, action.params.clipId);
    if (!clip) {
      errors.push({
        code: "CLIP_NOT_FOUND",
        message: `Clip with ID ${action.params.clipId} not found`,
        path: "params.clipId"
      });
      return errors;
    }
    const track = this.findTrack(timeline, clip.trackId);
    if (track?.locked) {
      errors.push({
        code: "TRACK_LOCKED",
        message: `Track containing clip ${action.params.clipId} is locked`,
        path: "params.clipId"
      });
    }
    switch (action.type) {
      case "effect/add":
        if (!action.params.effectType || typeof action.params.effectType !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Effect type is required and must be a string",
            path: "params.effectType"
          });
        }
        break;
      case "effect/remove":
      case "effect/update":
      case "effect/toggle":
      case "effect/reorder":
        if (!action.params.effectId || typeof action.params.effectId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Effect ID is required and must be a string",
            path: "params.effectId"
          });
        } else {
          const effectExists = clip.effects.some(
            (e) => e.id === action.params.effectId
          );
          if (!effectExists) {
            errors.push({
              code: "EFFECT_NOT_FOUND",
              message: `Effect with ID ${action.params.effectId} not found on clip`,
              path: "params.effectId"
            });
          }
        }
        if (action.type === "effect/reorder") {
          if (typeof action.params.newIndex !== "number" || action.params.newIndex < 0 || action.params.newIndex >= clip.effects.length) {
            errors.push({
              code: "INVALID_PARAMS",
              message: `New index must be between 0 and ${clip.effects.length - 1}`,
              path: "params.newIndex"
            });
          }
        }
        if (action.type === "effect/toggle" && typeof action.params.enabled !== "boolean") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Enabled parameter must be a boolean",
            path: "params.enabled"
          });
        }
        break;
    }
    return errors;
  }
  validateTransformAction(action, project) {
    const errors = [];
    const timeline = project.timeline;
    if (!action.params.clipId || typeof action.params.clipId !== "string") {
      errors.push({
        code: "INVALID_PARAMS",
        message: "Clip ID is required and must be a string",
        path: "params.clipId"
      });
      return errors;
    }
    const clip = this.findClip(timeline, action.params.clipId);
    if (!clip) {
      errors.push({
        code: "CLIP_NOT_FOUND",
        message: `Clip with ID ${action.params.clipId} not found`,
        path: "params.clipId"
      });
      return errors;
    }
    const track = this.findTrack(timeline, clip.trackId);
    if (track?.locked) {
      errors.push({
        code: "TRACK_LOCKED",
        message: `Track containing clip ${action.params.clipId} is locked`,
        path: "params.clipId"
      });
    }
    if (!action.params.transform || typeof action.params.transform !== "object") {
      errors.push({
        code: "INVALID_PARAMS",
        message: "Transform is required and must be an object",
        path: "params.transform"
      });
    }
    return errors;
  }
  validateKeyframeAction(action, project) {
    const errors = [];
    const timeline = project.timeline;
    if (action.type === "keyframe/setAll") {
      return errors;
    }
    if (!action.params.clipId || typeof action.params.clipId !== "string") {
      errors.push({
        code: "INVALID_PARAMS",
        message: "Clip ID is required and must be a string",
        path: "params.clipId"
      });
      return errors;
    }
    const clip = this.findClip(timeline, action.params.clipId);
    if (!clip) {
      errors.push({
        code: "CLIP_NOT_FOUND",
        message: `Clip with ID ${action.params.clipId} not found`,
        path: "params.clipId"
      });
      return errors;
    }
    const track = this.findTrack(timeline, clip.trackId);
    if (track?.locked) {
      errors.push({
        code: "TRACK_LOCKED",
        message: `Track containing clip ${action.params.clipId} is locked`,
        path: "params.clipId"
      });
    }
    if (!action.params.property || typeof action.params.property !== "string") {
      errors.push({
        code: "INVALID_PARAMS",
        message: "Property is required and must be a string",
        path: "params.property"
      });
    }
    if (typeof action.params.time !== "number" || action.params.time < 0) {
      errors.push({
        code: "INVALID_PARAMS",
        message: "Time is required and must be a non-negative number",
        path: "params.time"
      });
    }
    if (action.type === "keyframe/add" && action.params.easing !== void 0 && !EASING_TYPES.includes(action.params.easing)) {
      errors.push({
        code: "INVALID_PARAMS",
        message: "Easing must be a supported easing type",
        path: "params.easing"
      });
    }
    return errors;
  }
  validateTransitionAction(action, project) {
    const errors = [];
    const timeline = project.timeline;
    switch (action.type) {
      case "transition/add":
        if (!action.params.clipAId || typeof action.params.clipAId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Clip A ID is required and must be a string",
            path: "params.clipAId"
          });
        }
        if (!action.params.clipBId || typeof action.params.clipBId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Clip B ID is required and must be a string",
            path: "params.clipBId"
          });
        }
        if (typeof action.params.transitionType !== "string" || !TRANSITION_TYPES.includes(action.params.transitionType)) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Transition type must be supported",
            path: "params.transitionType"
          });
        }
        if (typeof action.params.duration !== "number" || action.params.duration <= 0) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Duration must be a positive number",
            path: "params.duration"
          });
        }
        if (action.params.clipAId && action.params.clipBId) {
          const clipA = this.findClip(timeline, action.params.clipAId);
          const clipB = this.findClip(timeline, action.params.clipBId);
          if (!clipA) {
            errors.push({
              code: "CLIP_NOT_FOUND",
              message: `Clip A with ID ${action.params.clipAId} not found`,
              path: "params.clipAId"
            });
          }
          if (!clipB) {
            errors.push({
              code: "CLIP_NOT_FOUND",
              message: `Clip B with ID ${action.params.clipBId} not found`,
              path: "params.clipBId"
            });
          }
          if (clipA && clipB) {
            if (clipA.trackId !== clipB.trackId) {
              errors.push({
                code: "INVALID_PARAMS",
                message: "Clips must be on the same track for transition",
                path: "params"
              });
            }
            const clipAEnd = clipA.startTime + clipA.duration;
            const clipBEnd = clipB.startTime + clipB.duration;
            const tolerance = 1e-3;
            const aBeforeB = Math.abs(clipAEnd - clipB.startTime) < tolerance;
            const bBeforeA = Math.abs(clipBEnd - clipA.startTime) < tolerance;
            if (!aBeforeB && !bBeforeA) {
              errors.push({
                code: "INVALID_PARAMS",
                message: "Clips must be adjacent for transition",
                path: "params"
              });
            }
          }
        }
        break;
      case "transition/set": {
        const transition = action.params.transition;
        if (!transition || typeof transition.id !== "string" || typeof transition.clipAId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "A valid transition with id and clipAId is required",
            path: "params.transition"
          });
        } else if (!this.findClip(timeline, transition.clipAId)) {
          errors.push({
            code: "CLIP_NOT_FOUND",
            message: `Clip A with ID ${transition.clipAId} not found`,
            path: "params.transition.clipAId"
          });
        }
        break;
      }
      case "transition/remove":
      case "transition/update":
        if (!action.params.transitionId || typeof action.params.transitionId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Transition ID is required and must be a string",
            path: "params.transitionId"
          });
        }
        if (action.type === "transition/update" && action.params.type !== void 0 && !TRANSITION_TYPES.includes(action.params.type)) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Transition type must be supported",
            path: "params.type"
          });
        }
        if (action.type === "transition/update" && action.params.duration !== void 0) {
          if (typeof action.params.duration !== "number" || action.params.duration <= 0) {
            errors.push({
              code: "INVALID_PARAMS",
              message: "Duration must be a positive number",
              path: "params.duration"
            });
          }
        }
        break;
    }
    return errors;
  }
  validateAudioAction(action, project) {
    const errors = [];
    const timeline = project.timeline;
    if (!action.params.clipId || typeof action.params.clipId !== "string") {
      errors.push({
        code: "INVALID_PARAMS",
        message: "Clip ID is required and must be a string",
        path: "params.clipId"
      });
      return errors;
    }
    const clip = this.findClip(timeline, action.params.clipId);
    if (!clip) {
      errors.push({
        code: "CLIP_NOT_FOUND",
        message: `Clip with ID ${action.params.clipId} not found`,
        path: "params.clipId"
      });
      return errors;
    }
    const track = this.findTrack(timeline, clip.trackId);
    if (track?.locked) {
      errors.push({
        code: "TRACK_LOCKED",
        message: `Track containing clip ${action.params.clipId} is locked`,
        path: "params.clipId"
      });
    }
    switch (action.type) {
      case "audio/setVolume":
        if (typeof action.params.volume !== "number" || action.params.volume < 0 || action.params.volume > 4) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Volume must be a number between 0 and 4",
            path: "params.volume"
          });
        }
        break;
      case "audio/setFade":
        if (action.params.fadeIn !== void 0 && (typeof action.params.fadeIn !== "number" || action.params.fadeIn < 0)) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Fade-in must be a non-negative number",
            path: "params.fadeIn"
          });
        }
        if (action.params.fadeOut !== void 0 && (typeof action.params.fadeOut !== "number" || action.params.fadeOut < 0)) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Fade-out must be a non-negative number",
            path: "params.fadeOut"
          });
        }
        break;
      case "audio/addAutomation":
        if (!Array.isArray(action.params.points)) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Automation points must be an array",
            path: "params.points"
          });
        }
        break;
      case "audio/addEffect": {
        const effect = action.params.effect;
        if (!effect || typeof effect.id !== "string" || typeof effect.type !== "string" || !effect.type) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "A valid effect object with an id and type is required",
            path: "params.effect"
          });
        }
        break;
      }
      case "audio/removeEffect":
      case "audio/updateEffect":
      case "audio/toggleEffect":
        if (!action.params.effectId || typeof action.params.effectId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Effect ID is required and must be a string",
            path: "params.effectId"
          });
        } else {
          const effectExists = (clip.audioEffects ?? []).some(
            (e) => e.id === action.params.effectId
          );
          if (!effectExists) {
            errors.push({
              code: "EFFECT_NOT_FOUND",
              message: `Audio effect with ID ${action.params.effectId} not found on clip`,
              path: "params.effectId"
            });
          }
        }
        if (action.type === "audio/toggleEffect" && typeof action.params.enabled !== "boolean") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Enabled parameter must be a boolean",
            path: "params.enabled"
          });
        }
        break;
    }
    return errors;
  }
  validateSubtitleAction(action, _project) {
    const errors = [];
    switch (action.type) {
      case "subtitle/import":
        if (!action.params.srtContent || typeof action.params.srtContent !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "SRT content is required and must be a string",
            path: "params.srtContent"
          });
        }
        break;
      case "subtitle/add":
        if (!action.params.text || typeof action.params.text !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Subtitle text is required and must be a string",
            path: "params.text"
          });
        }
        if (typeof action.params.startTime !== "number" || action.params.startTime < 0) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Start time must be a non-negative number",
            path: "params.startTime"
          });
        }
        if (typeof action.params.endTime !== "number" || action.params.endTime < 0) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "End time must be a non-negative number",
            path: "params.endTime"
          });
        }
        if (typeof action.params.startTime === "number" && typeof action.params.endTime === "number" && action.params.endTime <= action.params.startTime) {
          errors.push({
            code: "INVALID_TIME_RANGE",
            message: "End time must be greater than start time",
            path: "params"
          });
        }
        break;
      case "subtitle/update":
        if (!action.params.subtitleId || typeof action.params.subtitleId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Subtitle ID is required and must be a string",
            path: "params.subtitleId"
          });
        }
        if (action.params.startTime !== void 0 && (typeof action.params.startTime !== "number" || action.params.startTime < 0)) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Start time must be a non-negative number",
            path: "params.startTime"
          });
        }
        if (action.params.endTime !== void 0 && (typeof action.params.endTime !== "number" || action.params.endTime < 0)) {
          errors.push({
            code: "INVALID_PARAMS",
            message: "End time must be a non-negative number",
            path: "params.endTime"
          });
        }
        break;
      case "subtitle/remove":
        if (!action.params.subtitleId || typeof action.params.subtitleId !== "string") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Subtitle ID is required and must be a string",
            path: "params.subtitleId"
          });
        }
        break;
      case "subtitle/setStyle":
        if (!action.params.style || typeof action.params.style !== "object") {
          errors.push({
            code: "INVALID_PARAMS",
            message: "Style is required and must be an object",
            path: "params.style"
          });
        }
        break;
    }
    return errors;
  }
  findTrack(timeline, trackId) {
    return timeline.tracks.find((t) => t.id === trackId) || null;
  }
  findClip(timeline, clipId) {
    for (const track of timeline.tracks) {
      const clip = track.clips.find((c) => c.id === clipId);
      if (clip) return clip;
    }
    return null;
  }
};

// vendor/openreel/upstream/packages/core/src/actions/action-history.ts
var ACTION_DESCRIPTIONS = {
  "clip/add": () => "Add clip",
  "clip/remove": () => "Delete clip",
  "clip/move": () => "Move clip",
  "clip/trim": () => "Trim clip",
  "clip/split": () => "Split clip",
  "clip/rippleDelete": () => "Ripple delete",
  "clip/duplicate": () => "Duplicate clip",
  "clip/setBlendMode": () => "Change blend mode",
  "clip/setBlendOpacity": () => "Adjust blend opacity",
  "clip/setEmphasisAnimation": () => "Set emphasis animation",
  "clip/setColorGrading": () => "Color grading",
  "track/add": (params) => `Add ${params.trackType} track`,
  "track/duplicate": () => "Duplicate track",
  "track/remove": () => "Remove track",
  "track/rename": () => "Rename track",
  "effect/add": (params) => `Add ${params.effectType} effect`,
  "effect/remove": () => "Remove effect",
  "effect/update": () => "Update effect",
  "effect/toggle": () => "Toggle effect",
  "effect/setStack": () => "Replace effect stack",
  "transform/update": () => "Transform clip",
  "keyframe/add": (params) => `Add ${params.property} keyframe`,
  "keyframe/remove": () => "Remove keyframe",
  "keyframe/setAll": () => "Replace clip keyframes",
  "transition/add": (params) => `Add ${params.transitionType} transition`,
  "transition/set": () => "Add transition",
  "transition/update": () => "Update transition",
  "transition/remove": () => "Remove transition",
  "audio/setVolume": () => "Adjust volume",
  "audio/setFade": () => "Adjust fade",
  "audio/addEffect": () => "Add audio effect",
  "audio/removeEffect": () => "Remove audio effect",
  "audio/updateEffect": () => "Update audio effect",
  "audio/toggleEffect": () => "Toggle audio effect",
  "subtitle/add": () => "Add subtitle",
  "subtitle/remove": () => "Remove subtitle",
  "marker/add": () => "Add marker",
  "marker/remove": () => "Remove marker",
  "marker/update": () => "Update marker",
  "project/rename": () => "Rename project",
  "project/updateSettings": () => "Update settings",
  "project/setCanvasBackground": () => "Change canvas background",
  "project/registerGeneratedShader": () => "Add generated shader",
  "project/removeGeneratedShader": () => "Remove generated shader",
  "media/import": () => "Import media",
  "media/delete": () => "Delete media",
  "clip/closeGapBefore": () => "Close gap",
  "track/consolidate": () => "Remove gaps",
  "track/restorePositions": () => "Restore positions"
};
function getActionDescription(action) {
  const descFn = ACTION_DESCRIPTIONS[action.type];
  if (descFn) {
    return descFn(action.params);
  }
  const parts = action.type.split("/");
  return `${parts[0]}: ${parts[1] || "action"}`;
}
var AUTO_GROUPABLE_TYPES = /* @__PURE__ */ new Set([
  "transform/update",
  "effect/update",
  "audio/setVolume",
  "audio/setFade",
  "audio/updateEffect",
  "transition/update",
  "clip/setBlendOpacity",
  "clip/setColorGrading",
  "clip/move",
  "clip/trim",
  "clip/slip",
  "clip/slide",
  "clip/roll",
  "clip/setSpeed",
  "clip/setChromaKey",
  "clip/setStabilization",
  "keyframe/move",
  "keyframe/update",
  "project/updateSettings",
  "project/setCanvasBackground"
]);
var TARGET_PARAM_KEYS = [
  "clipId",
  "effectId",
  "trackId",
  "keyframeId",
  "transitionId",
  "subtitleId"
];
function getActionTargetId(action) {
  const params = action.params;
  for (const key of TARGET_PARAM_KEYS) {
    const value = params[key];
    if (typeof value === "string" && value.length > 0) {
      return value;
    }
  }
  return null;
}
var ActionHistory = class {
  undoStack = [];
  redoStack = [];
  maxHistorySize;
  currentGroupId = null;
  groupDepth = 0;
  snapshots = [];
  listeners = /* @__PURE__ */ new Set();
  lastActionTime = 0;
  autoGroupWindow = 100;
  constructor(maxHistorySize = 1e3) {
    this.maxHistorySize = maxHistorySize;
  }
  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  notify() {
    this.listeners.forEach((listener) => listener());
  }
  push(action, inverseAction = null) {
    const now = Date.now();
    const timeSinceLastAction = now - this.lastActionTime;
    this.lastActionTime = now;
    const lastEntry = this.undoStack.length > 0 ? this.undoStack[this.undoStack.length - 1] : null;
    let groupId = this.currentGroupId;
    if (!groupId && lastEntry && timeSinceLastAction < this.autoGroupWindow && lastEntry.action.type === action.type && AUTO_GROUPABLE_TYPES.has(action.type)) {
      const lastTarget = getActionTargetId(lastEntry.action);
      const currentTarget = getActionTargetId(action);
      if (lastTarget !== null && lastTarget === currentTarget) {
        groupId = lastEntry.groupId ?? `auto-${now}`;
        if (!lastEntry.groupId) {
          this.undoStack[this.undoStack.length - 1] = { ...lastEntry, groupId };
        }
      }
    }
    const entry = {
      action,
      inverseAction,
      timestamp: now,
      description: getActionDescription(action),
      groupId: groupId || void 0
    };
    this.undoStack.push(entry);
    this.redoStack = [];
    this.snapshots = this.snapshots.filter(
      (s) => s.stackIndex <= this.undoStack.length
    );
    if (this.undoStack.length > this.maxHistorySize) {
      this.undoStack.shift();
      this.snapshots = this.snapshots.map((s) => ({ ...s, stackIndex: s.stackIndex - 1 })).filter((s) => s.stackIndex >= 0);
    }
    this.notify();
  }
  beginGroup(_description) {
    if (this.currentGroupId) {
      this.groupDepth += 1;
      return this.currentGroupId;
    }
    this.currentGroupId = `group-${Date.now()}`;
    this.groupDepth = 1;
    return this.currentGroupId;
  }
  endGroup() {
    if (this.groupDepth > 1) {
      this.groupDepth -= 1;
      return;
    }
    this.groupDepth = 0;
    this.currentGroupId = null;
    this.notify();
  }
  setAutoGroupWindow(ms) {
    this.autoGroupWindow = ms;
  }
  undo() {
    const entry = this.undoStack.pop();
    if (entry) {
      this.redoStack.push(entry);
      this.notify();
      return entry.inverseAction;
    }
    return null;
  }
  undoGroup() {
    if (this.undoStack.length === 0) return [];
    const lastEntry = this.undoStack[this.undoStack.length - 1];
    const groupId = lastEntry.groupId;
    if (!groupId) {
      const action = this.undo();
      return action ? [action] : [];
    }
    const inverseActions = [];
    while (this.undoStack.length > 0 && this.undoStack[this.undoStack.length - 1].groupId === groupId) {
      const action = this.undo();
      if (action) inverseActions.push(action);
    }
    return inverseActions;
  }
  redo() {
    const entry = this.redoStack.pop();
    if (entry) {
      this.undoStack.push(entry);
      this.notify();
      return entry.action;
    }
    return null;
  }
  redoGroup() {
    if (this.redoStack.length === 0) return [];
    const nextEntry = this.redoStack[this.redoStack.length - 1];
    const groupId = nextEntry.groupId;
    if (!groupId) {
      const action = this.redo();
      return action ? [action] : [];
    }
    const actions = [];
    while (this.redoStack.length > 0 && this.redoStack[this.redoStack.length - 1].groupId === groupId) {
      const action = this.redo();
      if (action) actions.push(action);
    }
    return actions;
  }
  createSnapshot(name) {
    const snapshot = {
      id: `snapshot-${Date.now()}`,
      name,
      timestamp: Date.now(),
      stackIndex: this.undoStack.length
    };
    this.snapshots.push(snapshot);
    this.notify();
    return snapshot;
  }
  getSnapshots() {
    return [...this.snapshots];
  }
  deleteSnapshot(id) {
    const index = this.snapshots.findIndex((s) => s.id === id);
    if (index !== -1) {
      this.snapshots.splice(index, 1);
      this.notify();
      return true;
    }
    return false;
  }
  getDisplayHistory() {
    const result = [];
    const seen = /* @__PURE__ */ new Set();
    for (let i = this.undoStack.length - 1; i >= 0; i--) {
      const entry = this.undoStack[i];
      if (entry.groupId) {
        if (!seen.has(entry.groupId)) {
          seen.add(entry.groupId);
          result.push({ entry, isCurrent: i === this.undoStack.length - 1 });
        }
      } else {
        result.push({ entry, isCurrent: i === this.undoStack.length - 1 });
      }
    }
    return result.reverse();
  }
  canUndo() {
    return this.undoStack.length > 0;
  }
  canRedo() {
    return this.redoStack.length > 0;
  }
  getHistory() {
    return this.undoStack.map((entry) => entry.action);
  }
  getHistoryEntries() {
    return [...this.undoStack];
  }
  getRedoEntries() {
    return [...this.redoStack];
  }
  clear() {
    this.undoStack = [];
    this.redoStack = [];
    this.snapshots = [];
    this.currentGroupId = null;
    this.groupDepth = 0;
    this.notify();
  }
  getUndoStackSize() {
    return this.undoStack.length;
  }
  getRedoStackSize() {
    return this.redoStack.length;
  }
  peekUndo() {
    return this.undoStack.length > 0 ? this.undoStack[this.undoStack.length - 1] : null;
  }
  peekRedo() {
    return this.redoStack.length > 0 ? this.redoStack[this.redoStack.length - 1] : null;
  }
  getMaxHistorySize() {
    return this.maxHistorySize;
  }
  setMaxHistorySize(size) {
    this.maxHistorySize = size;
    while (this.undoStack.length > this.maxHistorySize) {
      this.undoStack.shift();
    }
  }
};

// vendor/openreel/upstream/packages/core/src/actions/inverse-action-generator.ts
var InverseActionGenerator = class {
  generate(action, projectBefore) {
    const type = action.type;
    const handler = getActionHandler(type);
    if (handler) {
      return handler.invert(action, projectBefore);
    }
    if (type.startsWith("project/")) {
      return this.generateProjectInverse(
        action,
        projectBefore
      );
    } else if (type.startsWith("media/")) {
      return this.generateMediaInverse(
        action,
        projectBefore
      );
    } else if (type.startsWith("track/")) {
      return this.generateTrackInverse(
        action,
        projectBefore
      );
    } else if (type.startsWith("clip/")) {
      return this.generateClipInverse(
        action,
        projectBefore
      );
    } else if (type.startsWith("effect/")) {
      return this.generateEffectInverse(
        action,
        projectBefore
      );
    } else if (type.startsWith("transform/")) {
      return this.generateTransformInverse(
        action,
        projectBefore
      );
    } else if (type.startsWith("keyframe/")) {
      return this.generateKeyframeInverse(
        action,
        projectBefore
      );
    } else if (type.startsWith("transition/")) {
      return this.generateTransitionInverse(
        action,
        projectBefore
      );
    } else if (type.startsWith("audio/")) {
      return this.generateAudioInverse(
        action,
        projectBefore
      );
    } else if (type.startsWith("subtitle/")) {
      return this.generateSubtitleInverse(
        action,
        projectBefore
      );
    } else if (type.startsWith("marker/")) {
      return this.generateMarkerInverse(
        action,
        projectBefore
      );
    }
    return null;
  }
  generateMarkerInverse(action, projectBefore) {
    const timeline = projectBefore.timeline;
    switch (action.type) {
      case "marker/add":
        return this.createInverseAction(action, "marker/remove", {
          markerId: "__LAST_ADDED__"
        });
      case "marker/remove": {
        const position = timeline.markers.findIndex(
          (m) => m.id === action.params.markerId
        );
        if (position === -1) return null;
        const marker = timeline.markers[position];
        return this.createInverseAction(action, "marker/restore", {
          marker: { ...marker },
          position
        });
      }
      case "marker/update": {
        const marker = timeline.markers.find(
          (m) => m.id === action.params.markerId
        );
        if (!marker) return null;
        return this.createInverseAction(action, "marker/update", {
          markerId: action.params.markerId,
          updates: {
            time: marker.time,
            label: marker.label,
            color: marker.color
          }
        });
      }
    }
  }
  createInverseAction(originalAction, type, params) {
    return {
      type,
      id: `inverse-${originalAction.id}`,
      timestamp: Date.now(),
      params
    };
  }
  generateProjectInverse(action, projectBefore) {
    switch (action.type) {
      case "project/rename":
        return this.createInverseAction(action, "project/rename", {
          name: projectBefore.name
        });
      case "project/updateSettings":
        return this.createInverseAction(action, "project/updateSettings", {
          ...projectBefore.settings
        });
      case "project/setCanvasBackground":
        return this.createInverseAction(action, "project/setCanvasBackground", {
          backgroundFillMode: projectBefore.timeline.backgroundFillMode,
          layoutBackgroundColor: projectBefore.timeline.layoutBackgroundColor
        });
      case "project/create":
        return null;
      case "project/registerGeneratedShader":
        return this.createInverseAction(
          action,
          "project/removeGeneratedShader",
          { shaderId: action.params.def.id }
        );
      case "project/removeGeneratedShader": {
        const removed = (projectBefore.generatedShaders ?? []).find(
          (def) => def.id === action.params.shaderId
        );
        if (!removed) return null;
        return this.createInverseAction(
          action,
          "project/registerGeneratedShader",
          { def: removed }
        );
      }
    }
  }
  generateMediaInverse(action, projectBefore) {
    switch (action.type) {
      case "media/import":
        return this.createInverseAction(action, "media/delete", {
          mediaId: "__LAST_ADDED__"
        });
      case "media/delete": {
        const deletedMedia = projectBefore.mediaLibrary.items.find(
          (item) => item.id === action.params.mediaId
        );
        if (!deletedMedia) return null;
        return this.createInverseAction(action, "media/restore", {
          mediaItem: this.cloneMediaItem(deletedMedia)
        });
      }
      case "media/rename": {
        const media = projectBefore.mediaLibrary.items.find(
          (item) => item.id === action.params.mediaId
        );
        if (!media) return null;
        return this.createInverseAction(action, "media/rename", {
          mediaId: action.params.mediaId,
          name: media.name
        });
      }
    }
  }
  generateTrackInverse(action, projectBefore) {
    const timeline = projectBefore.timeline;
    switch (action.type) {
      case "track/add":
        return this.createInverseAction(action, "track/remove", {
          trackId: "__LAST_ADDED__"
          // Special marker to be resolved
        });
      case "track/duplicate":
        return this.createInverseAction(action, "track/remove", {
          trackId: "__LAST_ADDED__"
        });
      case "track/remove": {
        const removedTrack = timeline.tracks.find(
          (t) => t.id === action.params.trackId
        );
        if (!removedTrack) return null;
        const position = timeline.tracks.findIndex(
          (t) => t.id === action.params.trackId
        );
        return this.createInverseAction(action, "track/restore", {
          track: this.cloneTrack(removedTrack),
          position,
          items: {
            textClips: this.cloneTrackItems(projectBefore.textClips, action.params.trackId),
            shapeClips: this.cloneTrackItems(
              projectBefore.shapeClips,
              action.params.trackId
            ),
            svgClips: this.cloneTrackItems(projectBefore.svgClips, action.params.trackId),
            stickerClips: this.cloneTrackItems(
              projectBefore.stickerClips,
              action.params.trackId
            ),
            adjustmentLayers: this.cloneTrackItems(
              projectBefore.adjustmentLayers,
              action.params.trackId
            ),
            motionInstances: this.cloneTrackItems(
              projectBefore.motionInstances,
              action.params.trackId
            )
          }
        });
      }
      case "track/rename": {
        const track = timeline.tracks.find(
          (t) => t.id === action.params.trackId
        );
        if (!track) return null;
        return this.createInverseAction(action, "track/rename", {
          trackId: action.params.trackId,
          name: track.name
        });
      }
      case "track/reorder": {
        const currentPosition = timeline.tracks.findIndex(
          (t) => t.id === action.params.trackId
        );
        if (currentPosition === -1) return null;
        return this.createInverseAction(action, "track/reorder", {
          trackId: action.params.trackId,
          newPosition: currentPosition
        });
      }
      case "track/lock": {
        const track = timeline.tracks.find(
          (t) => t.id === action.params.trackId
        );
        if (!track) return null;
        return this.createInverseAction(action, "track/lock", {
          trackId: action.params.trackId,
          locked: track.locked
        });
      }
      case "track/hide": {
        const track = timeline.tracks.find(
          (t) => t.id === action.params.trackId
        );
        if (!track) return null;
        return this.createInverseAction(action, "track/hide", {
          trackId: action.params.trackId,
          hidden: track.hidden
        });
      }
      case "track/mute": {
        const track = timeline.tracks.find(
          (t) => t.id === action.params.trackId
        );
        if (!track) return null;
        return this.createInverseAction(action, "track/mute", {
          trackId: action.params.trackId,
          muted: track.muted
        });
      }
      case "track/solo": {
        const track = timeline.tracks.find(
          (t) => t.id === action.params.trackId
        );
        if (!track) return null;
        return this.createInverseAction(action, "track/solo", {
          trackId: action.params.trackId,
          solo: track.solo
        });
      }
      case "track/consolidate": {
        const trackId = action.params.trackId;
        const track = timeline.tracks.find((t) => t.id === trackId);
        if (!track) return null;
        const positions = track.clips.map((c) => ({
          clipId: c.id,
          startTime: c.startTime
        }));
        return this.createInverseAction(action, "track/restorePositions", {
          trackId,
          positions
        });
      }
    }
    return null;
  }
  generateClipInverse(action, projectBefore) {
    const timeline = projectBefore.timeline;
    switch (action.type) {
      case "clip/add":
        return this.createInverseAction(action, "clip/remove", {
          clipId: "__LAST_ADDED__"
        });
      case "clip/remove": {
        const clip = this.findClip(timeline, action.params.clipId);
        if (!clip) return null;
        return this.createInverseAction(action, "clip/restore", {
          clip: this.cloneClip(clip)
        });
      }
      case "clip/restore":
        return this.createInverseAction(action, "clip/remove", {
          clipId: action.params.clip.id
        });
      case "clip/move": {
        const clip = this.findClip(timeline, action.params.clipId);
        if (!clip) return null;
        return this.createInverseAction(action, "clip/move", {
          clipId: action.params.clipId,
          startTime: clip.startTime,
          trackId: clip.trackId
        });
      }
      case "clip/trim": {
        const clip = this.findClip(timeline, action.params.clipId);
        if (!clip) return null;
        return this.createInverseAction(action, "clip/trim", {
          clipId: action.params.clipId,
          inPoint: clip.inPoint,
          outPoint: clip.outPoint
        });
      }
      case "clip/trimToPlayhead": {
        const clip = this.findClip(timeline, action.params.clipId);
        if (!clip) return null;
        return this.createInverseAction(action, "clip/restoreTiming", {
          clipId: clip.id,
          startTime: clip.startTime,
          duration: clip.duration,
          inPoint: clip.inPoint,
          outPoint: clip.outPoint
        });
      }
      case "clip/split": {
        const clip = this.findClip(timeline, action.params.clipId);
        if (!clip) return null;
        return this.createInverseAction(action, "clip/merge", {
          clipId: action.params.clipId,
          originalClip: this.cloneClip(clip)
        });
      }
      case "clip/rippleDelete": {
        const clip = this.findClip(timeline, action.params.clipId);
        if (!clip) return null;
        const track = timeline.tracks.find((t) => t.id === clip.trackId);
        const affectedClips = track?.clips.filter((c) => c.startTime > clip.startTime).map((c) => ({ id: c.id, originalStartTime: c.startTime })) || [];
        return this.createInverseAction(action, "clip/rippleRestore", {
          clip: this.cloneClip(clip),
          affectedClips
        });
      }
      case "clip/setBlendMode": {
        const clip = this.findClip(timeline, action.params.clipId);
        if (!clip) return null;
        return this.createInverseAction(action, "clip/setBlendMode", {
          clipId: action.params.clipId,
          blendMode: clip.blendMode ?? "normal"
        });
      }
      case "clip/setBlendOpacity": {
        const clip = this.findClip(timeline, action.params.clipId);
        if (!clip) return null;
        return this.createInverseAction(action, "clip/setBlendOpacity", {
          clipId: action.params.clipId,
          opacity: clip.blendOpacity ?? 100
        });
      }
      case "clip/setEmphasisAnimation": {
        const clip = this.findClip(timeline, action.params.clipId);
        if (!clip) return null;
        return this.createInverseAction(action, "clip/setEmphasisAnimation", {
          clipId: action.params.clipId,
          emphasisAnimation: clip.emphasisAnimation ? { ...clip.emphasisAnimation } : void 0
        });
      }
      case "clip/setColorGrading": {
        const clip = this.findClip(timeline, action.params.clipId);
        if (!clip) return null;
        return this.createInverseAction(action, "clip/setColorGrading", {
          clipId: action.params.clipId,
          colorGrading: clip.colorGrading ? { ...clip.colorGrading } : void 0
        });
      }
      case "clip/closeGapBefore": {
        const params = action.params;
        const clip = this.findClip(timeline, params.clipId);
        if (!clip) return null;
        const track = timeline.tracks.find((t) => t.id === clip.trackId);
        if (!track) return null;
        const positions = track.clips.filter((c) => c.startTime >= clip.startTime).map((c) => ({ clipId: c.id, startTime: c.startTime }));
        return this.createInverseAction(action, "track/restorePositions", {
          trackId: track.id,
          positions
        });
      }
    }
    return null;
  }
  generateEffectInverse(action, projectBefore) {
    const timeline = projectBefore.timeline;
    const clip = this.findClip(timeline, action.params.clipId);
    if (!clip) return null;
    switch (action.type) {
      case "effect/add":
        return this.createInverseAction(action, "effect/remove", {
          clipId: action.params.clipId,
          effectId: action.params.effectId ?? "__LAST_ADDED__"
        });
      case "effect/remove": {
        const effect = clip.effects.find(
          (e) => e.id === action.params.effectId
        );
        if (!effect) return null;
        const index = clip.effects.findIndex(
          (e) => e.id === action.params.effectId
        );
        return this.createInverseAction(action, "effect/restore", {
          clipId: action.params.clipId,
          effect: { ...effect },
          index
        });
      }
      case "effect/update": {
        const effect = clip.effects.find(
          (e) => e.id === action.params.effectId
        );
        if (!effect) return null;
        return this.createInverseAction(action, "effect/update", {
          clipId: action.params.clipId,
          effectId: action.params.effectId,
          params: { ...effect.params }
        });
      }
      case "effect/toggle": {
        const effect = clip.effects.find(
          (e) => e.id === action.params.effectId
        );
        if (!effect) return null;
        return this.createInverseAction(action, "effect/toggle", {
          clipId: action.params.clipId,
          effectId: action.params.effectId,
          enabled: effect.enabled
        });
      }
      case "effect/reorder": {
        const currentIndex = clip.effects.findIndex(
          (e) => e.id === action.params.effectId
        );
        if (currentIndex === -1) return null;
        return this.createInverseAction(action, "effect/reorder", {
          clipId: action.params.clipId,
          effectId: action.params.effectId,
          newIndex: currentIndex
        });
      }
    }
    return null;
  }
  generateTransformInverse(action, projectBefore) {
    const clip = this.findClip(projectBefore.timeline, action.params.clipId);
    if (!clip) return null;
    return this.createInverseAction(action, "transform/update", {
      clipId: action.params.clipId,
      transform: { ...clip.transform }
    });
  }
  generateKeyframeInverse(action, projectBefore) {
    const clip = this.findClip(projectBefore.timeline, action.params.clipId);
    if (!clip) return null;
    switch (action.type) {
      case "keyframe/add":
        return this.createInverseAction(action, "keyframe/remove", {
          clipId: action.params.clipId,
          property: action.params.property,
          time: action.params.time
        });
      case "keyframe/remove": {
        const keyframe = clip.keyframes.find(
          (kf) => kf.property === action.params.property && kf.time === action.params.time
        );
        if (!keyframe) return null;
        return this.createInverseAction(action, "keyframe/add", {
          clipId: action.params.clipId,
          property: keyframe.property,
          time: keyframe.time,
          value: keyframe.value
        });
      }
      case "keyframe/update": {
        const keyframe = clip.keyframes.find(
          (kf) => kf.property === action.params.property && kf.time === action.params.time
        );
        if (!keyframe) return null;
        return this.createInverseAction(action, "keyframe/update", {
          clipId: action.params.clipId,
          property: keyframe.property,
          time: keyframe.time,
          value: keyframe.value,
          easing: keyframe.easing
        });
      }
    }
    return null;
  }
  generateTransitionInverse(action, projectBefore) {
    const timeline = projectBefore.timeline;
    switch (action.type) {
      case "transition/add":
        return this.createInverseAction(action, "transition/remove", {
          transitionId: "__LAST_ADDED__"
        });
      case "transition/set": {
        const clipA = this.findClip(
          timeline,
          action.params.transition.clipAId
        );
        if (!clipA) return null;
        const track = timeline.tracks.find((t) => t.id === clipA.trackId);
        if (!track) return null;
        return this.createInverseAction(action, "transition/restoreTrack", {
          trackId: track.id,
          transitions: (track.transitions || []).map((t) => ({ ...t }))
        });
      }
      case "transition/remove": {
        const transition = this.findTransition(
          timeline,
          action.params.transitionId
        );
        if (!transition) return null;
        return this.createInverseAction(action, "transition/restore", {
          transition: { ...transition }
        });
      }
      case "transition/update": {
        const transition = this.findTransition(
          timeline,
          action.params.transitionId
        );
        if (!transition) return null;
        return this.createInverseAction(action, "transition/update", {
          transitionId: action.params.transitionId,
          type: transition.type,
          duration: transition.duration,
          params: { ...transition.params }
        });
      }
    }
  }
  generateAudioInverse(action, projectBefore) {
    const clip = this.findClip(projectBefore.timeline, action.params.clipId);
    if (!clip) return null;
    switch (action.type) {
      case "audio/setVolume":
        return this.createInverseAction(action, "audio/setVolume", {
          clipId: action.params.clipId,
          volume: clip.volume
        });
      case "audio/setFade":
        return this.createInverseAction(action, "audio/setFade", {
          clipId: action.params.clipId,
          fadeIn: clip.fade?.fadeIn ?? 0,
          fadeOut: clip.fade?.fadeOut ?? 0
        });
      case "audio/addAutomation":
        return this.createInverseAction(action, "audio/addAutomation", {
          clipId: action.params.clipId,
          points: clip.automation?.volume ?? []
        });
      case "audio/addEffect": {
        const effect = action.params.effect;
        return this.createInverseAction(action, "audio/removeEffect", {
          clipId: action.params.clipId,
          effectId: effect.id
        });
      }
      case "audio/removeEffect": {
        const index = clip.audioEffects.findIndex(
          (e) => e.id === action.params.effectId
        );
        if (index === -1) return null;
        const effect = clip.audioEffects[index];
        return this.createInverseAction(action, "audio/restoreEffect", {
          clipId: action.params.clipId,
          effect: { ...effect, params: { ...effect.params } },
          index
        });
      }
      case "audio/updateEffect": {
        const effect = clip.audioEffects.find(
          (e) => e.id === action.params.effectId
        );
        if (!effect) return null;
        return this.createInverseAction(action, "audio/updateEffect", {
          clipId: action.params.clipId,
          effectId: action.params.effectId,
          params: { ...effect.params }
        });
      }
      case "audio/toggleEffect": {
        const effect = clip.audioEffects.find(
          (e) => e.id === action.params.effectId
        );
        if (!effect) return null;
        return this.createInverseAction(action, "audio/toggleEffect", {
          clipId: action.params.clipId,
          effectId: action.params.effectId,
          enabled: effect.enabled
        });
      }
    }
  }
  generateSubtitleInverse(action, projectBefore) {
    const timeline = projectBefore.timeline;
    switch (action.type) {
      case "subtitle/import":
        return this.createInverseAction(action, "subtitle/restoreAll", {
          subtitles: timeline.subtitles.map((s) => ({ ...s }))
        });
      case "subtitle/add":
        return this.createInverseAction(action, "subtitle/remove", {
          subtitleId: "__LAST_ADDED__"
        });
      case "subtitle/remove": {
        const subtitle = timeline.subtitles.find(
          (s) => s.id === action.params.subtitleId
        );
        if (!subtitle) return null;
        return this.createInverseAction(action, "subtitle/restore", {
          subtitle: { ...subtitle }
        });
      }
      case "subtitle/update": {
        const subtitle = timeline.subtitles.find(
          (s) => s.id === action.params.subtitleId
        );
        if (!subtitle) return null;
        return this.createInverseAction(action, "subtitle/update", {
          subtitleId: action.params.subtitleId,
          text: subtitle.text,
          startTime: subtitle.startTime,
          endTime: subtitle.endTime
        });
      }
      case "subtitle/setStyle": {
        const firstSubtitle = timeline.subtitles[0];
        return this.createInverseAction(action, "subtitle/setStyle", {
          style: firstSubtitle?.style ?? null
        });
      }
    }
    return null;
  }
  findClip(timeline, clipId) {
    for (const track of timeline.tracks) {
      const clip = track.clips.find((c) => c.id === clipId);
      if (clip) return clip;
    }
    return null;
  }
  findTransition(timeline, transitionId) {
    for (const track of timeline.tracks) {
      const transition = track.transitions?.find((t) => t.id === transitionId);
      if (transition) return transition;
    }
    return null;
  }
  cloneMediaItem(item) {
    return {
      id: item.id,
      name: item.name,
      type: item.type,
      fileHandle: item.fileHandle,
      blob: item.blob,
      metadata: { ...item.metadata },
      thumbnailUrl: item.thumbnailUrl,
      waveformData: item.waveformData ? new Float32Array(item.waveformData) : null
    };
  }
  cloneTrackItems(items, trackId) {
    return (items ?? []).filter((item) => item.trackId === trackId).map((item) => structuredClone(item));
  }
  cloneTrack(track) {
    return {
      ...track,
      clips: track.clips.map((c) => this.cloneClip(c)),
      transitions: track.transitions?.map((t) => ({ ...t })) ?? []
    };
  }
  cloneClip(clip) {
    return {
      ...clip,
      effects: clip.effects.map((e) => ({ ...e, params: { ...e.params } })),
      audioEffects: clip.audioEffects.map((e) => ({
        ...e,
        params: { ...e.params }
      })),
      transform: { ...clip.transform },
      fade: clip.fade ? { ...clip.fade } : void 0,
      automation: clip.automation ? {
        volume: clip.automation.volume?.map((p) => ({ ...p })),
        pan: clip.automation.pan?.map((p) => ({ ...p }))
      } : void 0,
      keyframes: clip.keyframes.map((kf) => ({ ...kf })),
      emphasisAnimation: clip.emphasisAnimation ? { ...clip.emphasisAnimation } : void 0,
      metadata: clip.metadata ? { ...clip.metadata } : void 0
    };
  }
};

// vendor/openreel/upstream/packages/core/src/actions/handlers/clip-helpers.ts
function findClip(project, clipId) {
  for (const track of project.timeline.tracks) {
    const clip = track.clips.find((c) => c.id === clipId);
    if (clip) return clip;
  }
  return void 0;
}
function patchClip(project, clipId, patch) {
  const timeline = project.timeline;
  let found = false;
  timeline.tracks = timeline.tracks.map((track) => ({
    ...track,
    clips: track.clips.map((clip) => {
      if (clip.id === clipId) {
        found = true;
        return { ...clip, ...patch };
      }
      return clip;
    })
  }));
  return found;
}

// vendor/openreel/upstream/packages/core/src/actions/handlers/clip-timing.ts
var restoreTiming = {
  type: "clip/restoreTiming",
  validate(action, project) {
    const params = action.params;
    const errors = [];
    if (typeof params.clipId !== "string" || !findClip(project, params.clipId)) {
      errors.push({ code: "CLIP_NOT_FOUND", message: "Clip not found" });
    }
    for (const field of ["startTime", "duration", "inPoint", "outPoint"]) {
      const value = params[field];
      if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
        errors.push({ code: "INVALID_PARAMS", message: `${field} must be a finite non-negative number` });
      }
    }
    if (typeof params.inPoint === "number" && typeof params.outPoint === "number" && params.outPoint < params.inPoint) {
      errors.push({ code: "INVALID_PARAMS", message: "Out point must follow in point" });
    }
    return { valid: errors.length === 0, errors };
  },
  apply(action, project) {
    const { clipId, startTime, duration, inPoint, outPoint } = action.params;
    patchClip(project, clipId, { startTime, duration, inPoint, outPoint });
  },
  invert(action, projectBefore) {
    const clip = findClip(projectBefore, action.params.clipId);
    if (!clip) return null;
    return {
      type: "clip/restoreTiming",
      id: `inverse-${action.id}`,
      timestamp: Date.now(),
      params: {
        clipId: clip.id,
        startTime: clip.startTime,
        duration: clip.duration,
        inPoint: clip.inPoint,
        outPoint: clip.outPoint
      }
    };
  }
};
registerActionHandler(restoreTiming);

// vendor/openreel/upstream/packages/core/src/timeline/project-duration.ts
function timedEnd(item) {
  const startTime = Number.isFinite(item.startTime) ? item.startTime : 0;
  const duration = Number.isFinite(item.duration) ? item.duration : 0;
  return Math.max(0, startTime) + Math.max(0, duration);
}
function calculateProjectDuration(project) {
  let maxEnd = 0;
  const include = (items) => {
    for (const item of items ?? []) maxEnd = Math.max(maxEnd, timedEnd(item));
  };
  for (const track of project.timeline.tracks) include(track.clips);
  include(project.textClips);
  include(project.shapeClips);
  include(project.svgClips);
  include(project.stickerClips);
  include(project.adjustmentLayers);
  include(project.nestedInstances);
  include(project.motionInstances);
  for (const subtitle of project.timeline.subtitles ?? []) {
    const endTime = Number.isFinite(subtitle.endTime) ? subtitle.endTime : 0;
    maxEnd = Math.max(maxEnd, Math.max(0, endTime));
  }
  return maxEnd;
}

// vendor/openreel/upstream/packages/core/src/actions/action-executor.ts
function getTrackOwnedItems(project, trackId) {
  return {
    textClips: (project.textClips ?? []).filter((item) => item.trackId === trackId),
    shapeClips: (project.shapeClips ?? []).filter((item) => item.trackId === trackId),
    svgClips: (project.svgClips ?? []).filter((item) => item.trackId === trackId),
    stickerClips: (project.stickerClips ?? []).filter(
      (item) => item.trackId === trackId
    ),
    adjustmentLayers: (project.adjustmentLayers ?? []).filter(
      (item) => item.trackId === trackId
    ),
    motionInstances: (project.motionInstances ?? []).filter(
      (item) => item.trackId === trackId
    )
  };
}
function removeTrackOwnedItems(project, trackId) {
  const mutable = project;
  mutable.textClips = (project.textClips ?? []).filter(
    (item) => item.trackId !== trackId
  );
  mutable.shapeClips = (project.shapeClips ?? []).filter(
    (item) => item.trackId !== trackId
  );
  mutable.svgClips = (project.svgClips ?? []).filter(
    (item) => item.trackId !== trackId
  );
  mutable.stickerClips = (project.stickerClips ?? []).filter(
    (item) => item.trackId !== trackId
  );
  mutable.adjustmentLayers = (project.adjustmentLayers ?? []).filter(
    (item) => item.trackId !== trackId
  );
  mutable.motionInstances = (project.motionInstances ?? []).filter(
    (item) => item.trackId !== trackId
  );
}
function restoreTrackOwnedItems(project, items) {
  const mutable = project;
  mutable.textClips = [...project.textClips ?? [], ...items.textClips];
  mutable.shapeClips = [...project.shapeClips ?? [], ...items.shapeClips];
  mutable.svgClips = [...project.svgClips ?? [], ...items.svgClips];
  mutable.stickerClips = [...project.stickerClips ?? [], ...items.stickerClips];
  mutable.adjustmentLayers = [
    ...project.adjustmentLayers ?? [],
    ...items.adjustmentLayers
  ];
  mutable.motionInstances = [
    ...project.motionInstances ?? [],
    ...items.motionInstances
  ];
}
var ActionExecutor = class {
  validator;
  history;
  inverseGenerator;
  lastAddedIds = /* @__PURE__ */ new Map();
  isSameTransitionPlacement(candidate, transition) {
    if (candidate.id === transition.id) {
      return true;
    }
    if (candidate.clipBId || transition.clipBId) {
      return candidate.clipAId === transition.clipAId && candidate.clipBId === transition.clipBId;
    }
    return candidate.clipAId === transition.clipAId && (candidate.edge ?? "out") === (transition.edge ?? "out");
  }
  constructor(history) {
    this.validator = new ActionValidator();
    this.history = history || new ActionHistory();
    this.inverseGenerator = new InverseActionGenerator();
  }
  async execute(action, project) {
    const validationResult = this.validator.validate(action, project);
    if (!validationResult.valid) {
      return {
        success: false,
        error: {
          code: "INVALID_PARAMS",
          message: validationResult.errors.map((e) => e.message).join("; "),
          details: { errors: validationResult.errors }
        }
      };
    }
    const projectSnapshot = {
      ...JSON.parse(JSON.stringify(project)),
      // Runtime media must survive snapshot undo; Blob/File handles are not JSON.
      mediaLibrary: { items: project.mediaLibrary.items.map((item) => ({ ...item })) }
    };
    const inverseAction = this.inverseGenerator.generate(
      action,
      projectSnapshot
    );
    try {
      await this.applyAction(action, project);
      const resolvedInverseAction = inverseAction ? this.resolveSpecialMarkers(inverseAction) : null;
      this.history.push(action, resolvedInverseAction);
      return {
        success: true,
        actionId: action.id
      };
    } catch (error) {
      return {
        success: false,
        error: {
          code: "INVALID_PARAMS",
          message: error instanceof Error ? error.message : "Unknown error occurred"
        }
      };
    }
  }
  async executeMany(actions, project) {
    const results = [];
    for (const action of actions) {
      const result = await this.execute(action, project);
      results.push(result);
      if (!result.success) {
        break;
      }
    }
    return results;
  }
  async undo(project) {
    if (!this.history.canUndo()) {
      return {
        success: false,
        error: {
          code: "INVALID_PARAMS",
          message: "Nothing to undo"
        }
      };
    }
    const inverseActions = this.history.undoGroup();
    if (inverseActions.length === 0) {
      return {
        success: false,
        error: {
          code: "INVALID_PARAMS",
          message: "No inverse action available"
        }
      };
    }
    try {
      for (const inverseAction of inverseActions) {
        const resolvedAction = this.resolveSpecialMarkers(inverseAction);
        await this.applyAction(resolvedAction, project);
      }
      return { success: true, actionId: inverseActions[0].id };
    } catch (error) {
      return {
        success: false,
        error: {
          code: "INVALID_PARAMS",
          message: error instanceof Error ? error.message : "Undo failed"
        }
      };
    }
  }
  async redo(project) {
    if (!this.history.canRedo()) {
      return {
        success: false,
        error: {
          code: "INVALID_PARAMS",
          message: "Nothing to redo"
        }
      };
    }
    const actions = this.history.redoGroup();
    if (actions.length === 0) {
      return {
        success: false,
        error: {
          code: "INVALID_PARAMS",
          message: "No action to redo"
        }
      };
    }
    try {
      for (const action of actions) {
        await this.applyAction(action, project);
      }
      return { success: true, actionId: actions[0].id };
    } catch (error) {
      return {
        success: false,
        error: {
          code: "INVALID_PARAMS",
          message: error instanceof Error ? error.message : "Redo failed"
        }
      };
    }
  }
  getHistory() {
    return this.history;
  }
  resolveSpecialMarkers(action) {
    const params = { ...action.params };
    for (const [key, value] of Object.entries(params)) {
      if (value === "__LAST_ADDED__") {
        const lastId = this.lastAddedIds.get(action.type.split("/")[0]);
        if (lastId) {
          params[key] = lastId;
        }
      }
    }
    return { ...action, params };
  }
  async applyAction(action, project) {
    const type = action.type;
    const handler = getActionHandler(type);
    if (handler) {
      await handler.apply(action, project, {
        lastAddedIds: this.lastAddedIds
      });
      this.recalculateTimelineDuration(project);
      this.markProjectModified(project);
      return;
    }
    if (type.startsWith("project/")) {
      this.applyProjectAction(action, project);
    } else if (type.startsWith("media/")) {
      await this.applyMediaAction(action, project);
    } else if (type.startsWith("track/")) {
      this.applyTrackAction(action, project);
    } else if (type.startsWith("clip/")) {
      this.applyClipAction(action, project);
    } else if (type.startsWith("effect/")) {
      this.applyEffectAction(action, project);
    } else if (type.startsWith("transform/")) {
      this.applyTransformAction(action, project);
    } else if (type.startsWith("keyframe/")) {
      this.applyKeyframeAction(action, project);
    } else if (type.startsWith("transition/")) {
      this.applyTransitionAction(action, project);
    } else if (type.startsWith("audio/")) {
      this.applyAudioAction(action, project);
    } else if (type.startsWith("subtitle/")) {
      this.applySubtitleAction(action, project);
    } else if (type.startsWith("marker/")) {
      this.applyMarkerAction(action, project);
    }
    this.recalculateTimelineDuration(project);
    this.markProjectModified(project);
  }
  recalculateTimelineDuration(project) {
    project.timeline.duration = calculateProjectDuration(project);
  }
  markProjectModified(project) {
    project.modifiedAt = Date.now();
  }
  applyProjectAction(action, project) {
    switch (action.type) {
      case "project/rename":
        project.name = action.params.name;
        project.modifiedAt = Date.now();
        break;
      case "project/updateSettings":
        project.settings = {
          ...project.settings,
          ...action.params
        };
        project.modifiedAt = Date.now();
        break;
      case "project/setCanvasBackground":
        project.timeline = {
          ...project.timeline,
          backgroundFillMode: action.params.backgroundFillMode,
          layoutBackgroundColor: action.params.layoutBackgroundColor
        };
        project.modifiedAt = Date.now();
        break;
      case "project/create":
        project.name = action.params.name;
        project.settings = action.params.settings;
        project.modifiedAt = Date.now();
        break;
      case "project/registerGeneratedShader": {
        const def = action.params.def;
        if (isBuiltinMotionShaderId(def.id)) break;
        const existing = project.generatedShaders ?? [];
        const next = [
          ...existing.filter((entry) => entry.id !== def.id),
          def
        ];
        project.generatedShaders = next;
        project.modifiedAt = Date.now();
        registerMotionShader(def);
        break;
      }
      case "project/removeGeneratedShader": {
        const shaderId = action.params.shaderId;
        const existing = project.generatedShaders ?? [];
        project.generatedShaders = existing.filter(
          (entry) => entry.id !== shaderId
        );
        project.modifiedAt = Date.now();
        unregisterMotionShader(shaderId);
        break;
      }
    }
  }
  async applyMediaAction(action, project) {
    const mediaLibrary = project.mediaLibrary;
    switch (action.type) {
      case "media/import": {
        const params = action.params;
        const newMediaItem = {
          id: `media-${Date.now()}`,
          name: params.file.name,
          type: this.inferMediaType(params.file),
          fileHandle: null,
          blob: params.file,
          metadata: {
            duration: 0,
            width: 0,
            height: 0,
            frameRate: 0,
            codec: "",
            sampleRate: 0,
            channels: 0,
            fileSize: params.file.size
          },
          thumbnailUrl: null,
          waveformData: null
        };
        mediaLibrary.items = [...mediaLibrary.items, newMediaItem];
        this.lastAddedIds.set("media", newMediaItem.id);
        break;
      }
      case "media/delete": {
        const params = action.params;
        mediaLibrary.items = mediaLibrary.items.filter(
          (item) => item.id !== params.mediaId
        );
        break;
      }
      case "media/rename": {
        const params = action.params;
        mediaLibrary.items = mediaLibrary.items.map(
          (item) => item.id === params.mediaId ? { ...item, name: params.name } : item
        );
        break;
      }
      case "media/restore": {
        const params = action.params;
        mediaLibrary.items = [...mediaLibrary.items, params.mediaItem];
        break;
      }
    }
  }
  inferMediaType(file) {
    if (file.type.startsWith("video/")) return "video";
    if (file.type.startsWith("audio/")) return "audio";
    if (file.type.startsWith("image/")) return "image";
    return "video";
  }
  applyTrackAction(action, project) {
    const timeline = project.timeline;
    switch (action.type) {
      case "track/add": {
        const params = action.params;
        const trackNames = {
          video: "Video",
          audio: "Audio",
          image: "Image",
          text: "Text",
          graphics: "Graphics"
        };
        const trackCount = timeline.tracks.filter(
          (track) => params.mode === "standard" ? track.mode === "standard" : track.type === params.trackType
        ).length + 1;
        const newTrack = {
          id: params.trackId ?? `track-${crypto.randomUUID()}`,
          type: params.trackType,
          mode: params.mode,
          role: params.role,
          name: params.name ?? `${params.mode === "standard" ? "Track" : trackNames[params.trackType] || params.trackType} ${trackCount}`,
          clips: [],
          transitions: [],
          locked: false,
          hidden: false,
          muted: false,
          solo: false
        };
        const position = params.position !== void 0 ? params.position : timeline.tracks.length;
        timeline.tracks = [
          ...timeline.tracks.slice(0, position),
          newTrack,
          ...timeline.tracks.slice(position)
        ];
        this.lastAddedIds.set("track", newTrack.id);
        break;
      }
      case "track/duplicate": {
        const params = action.params;
        const sourceIndex = timeline.tracks.findIndex(
          (track) => track.id === params.sourceTrackId
        );
        if (sourceIndex < 0) break;
        const source = timeline.tracks[sourceIndex];
        const trackId = params.trackId ?? `track-${crypto.randomUUID()}`;
        params.trackId = trackId;
        const ownedItems = getTrackOwnedItems(project, source.id);
        const sourceItemIds = [
          ...source.clips.map((clip) => clip.id),
          ...ownedItems.textClips.map((item) => item.id),
          ...ownedItems.shapeClips.map((item) => item.id),
          ...ownedItems.svgClips.map((item) => item.id),
          ...ownedItems.stickerClips.map((item) => item.id),
          ...ownedItems.adjustmentLayers.map((item) => item.id),
          ...ownedItems.motionInstances.map((item) => item.id)
        ];
        params.itemIdMap ??= Object.fromEntries(
          sourceItemIds.map((id) => [id, `item-${crypto.randomUUID()}`])
        );
        params.transitionIdMap ??= Object.fromEntries(
          source.transitions.map((transition) => [
            transition.id,
            `transition-${crypto.randomUUID()}`
          ])
        );
        const duplicateItem = (item) => ({
          ...structuredClone(item),
          id: params.itemIdMap[item.id],
          trackId
        });
        const names = new Set(timeline.tracks.map((track) => track.name));
        const baseName = `${source.name} Copy`;
        let name = baseName;
        let suffix = 2;
        while (names.has(name)) {
          name = `${baseName} ${suffix}`;
          suffix += 1;
        }
        const duplicate = {
          ...structuredClone(source),
          id: trackId,
          name,
          clips: source.clips.map((clip) => ({
            ...structuredClone(clip),
            id: params.itemIdMap[clip.id],
            trackId
          })),
          transitions: source.transitions.map((transition) => ({
            ...structuredClone(transition),
            id: params.transitionIdMap[transition.id],
            clipAId: params.itemIdMap[transition.clipAId] ?? transition.clipAId,
            clipBId: transition.clipBId ? params.itemIdMap[transition.clipBId] ?? transition.clipBId : void 0
          }))
        };
        const position = params.position ?? sourceIndex + 1;
        timeline.tracks = [
          ...timeline.tracks.slice(0, position),
          duplicate,
          ...timeline.tracks.slice(position)
        ];
        restoreTrackOwnedItems(project, {
          textClips: ownedItems.textClips.map(duplicateItem),
          shapeClips: ownedItems.shapeClips.map(duplicateItem),
          svgClips: ownedItems.svgClips.map(duplicateItem),
          stickerClips: ownedItems.stickerClips.map(duplicateItem),
          adjustmentLayers: ownedItems.adjustmentLayers.map((layer) => ({
            ...duplicateItem(layer),
            affectedTracks: layer.affectedTracks === "all" ? "all" : layer.affectedTracks.map(
              (affectedTrackId) => affectedTrackId === source.id ? trackId : affectedTrackId
            )
          })),
          motionInstances: ownedItems.motionInstances.map(duplicateItem)
        });
        this.lastAddedIds.set("track", trackId);
        break;
      }
      case "track/remove": {
        const params = action.params;
        timeline.tracks = timeline.tracks.filter(
          (t) => t.id !== params.trackId
        );
        removeTrackOwnedItems(project, params.trackId);
        break;
      }
      case "track/restore": {
        const params = action.params;
        timeline.tracks = [
          ...timeline.tracks.slice(0, params.position),
          params.track,
          ...timeline.tracks.slice(params.position)
        ];
        if (params.items) {
          restoreTrackOwnedItems(project, params.items);
        }
        break;
      }
      case "track/reorder": {
        const params = action.params;
        const trackIndex = timeline.tracks.findIndex(
          (t) => t.id === params.trackId
        );
        if (trackIndex !== -1) {
          const [track] = timeline.tracks.splice(trackIndex, 1);
          timeline.tracks.splice(params.newPosition, 0, track);
        }
        break;
      }
      case "track/lock": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map(
          (t) => t.id === params.trackId ? { ...t, locked: params.locked } : t
        );
        break;
      }
      case "track/hide": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map(
          (t) => t.id === params.trackId ? { ...t, hidden: params.hidden } : t
        );
        break;
      }
      case "track/rename": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map(
          (t) => t.id === params.trackId ? { ...t, name: params.name } : t
        );
        break;
      }
      case "track/mute": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map(
          (t) => t.id === params.trackId ? { ...t, muted: params.muted } : t
        );
        break;
      }
      case "track/solo": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map(
          (t) => t.id === params.trackId ? { ...t, solo: params.solo } : t
        );
        break;
      }
    }
  }
  applyMarkerAction(action, project) {
    const timeline = project.timeline;
    switch (action.type) {
      case "marker/add": {
        const params = action.params;
        const newMarker = {
          id: `marker-${Date.now()}-${Math.random().toString(36).slice(2, 11)}`,
          time: params.time,
          label: params.label,
          color: params.color
        };
        timeline.markers = [...timeline.markers, newMarker];
        this.lastAddedIds.set("marker", newMarker.id);
        break;
      }
      case "marker/restore": {
        const params = action.params;
        const markers = [...timeline.markers];
        const position = Math.min(
          Math.max(params.position, 0),
          markers.length
        );
        markers.splice(position, 0, { ...params.marker });
        timeline.markers = markers;
        break;
      }
      case "marker/remove": {
        const params = action.params;
        timeline.markers = timeline.markers.filter(
          (m) => m.id !== params.markerId
        );
        break;
      }
      case "marker/update": {
        const params = action.params;
        timeline.markers = timeline.markers.map(
          (m) => m.id === params.markerId ? { ...m, ...params.updates } : m
        );
        break;
      }
    }
  }
  applyClipAction(action, project) {
    const timeline = project.timeline;
    switch (action.type) {
      case "clip/add": {
        const params = action.params;
        const track = timeline.tracks.find(
          (t) => t.id === params.trackId
        );
        if (track) {
          const mediaItem = project.mediaLibrary.items.find(
            (item) => item.id === params.mediaId
          );
          const clipDuration = params.duration ?? (mediaItem?.metadata.duration && mediaItem.metadata.duration > 0 ? mediaItem.metadata.duration : 5);
          const defaultTransform = {
            position: { x: 0, y: 0 },
            scale: { x: 1, y: 1 },
            rotation: 0,
            anchor: { x: 0.5, y: 0.5 },
            opacity: 1,
            fitMode: "contain"
          };
          const newClip = params.sourceClip ? {
            ...structuredClone(params.sourceClip),
            id: params.clipId ?? crypto.randomUUID(),
            trackId: params.trackId,
            startTime: params.startTime
          } : {
            id: params.clipId ?? crypto.randomUUID(),
            mediaId: params.mediaId,
            trackId: params.trackId,
            startTime: params.startTime,
            duration: clipDuration,
            inPoint: params.inPoint ?? 0,
            outPoint: params.outPoint ?? clipDuration,
            effects: params.effects ?? [],
            audioEffects: params.audioEffects ?? [],
            transform: params.transform ? { ...defaultTransform, ...params.transform } : defaultTransform,
            volume: params.volume ?? 1,
            keyframes: params.keyframes ?? [],
            ...params.fade ? { fade: params.fade } : {},
            ...params.speed !== void 0 ? { speed: params.speed } : {},
            ...params.reversed !== void 0 ? { reversed: params.reversed } : {},
            ...params.audioTrackIndex !== void 0 ? { audioTrackIndex: params.audioTrackIndex } : {}
          };
          params.clipId = newClip.id;
          track.clips = [...track.clips, newClip];
          this.lastAddedIds.set("clip", newClip.id);
        }
        break;
      }
      case "clip/remove": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.filter((c) => c.id !== params.clipId)
        }));
        break;
      }
      case "clip/restore": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map(
          (track) => track.id === params.clip.trackId ? { ...track, clips: [...track.clips, params.clip] } : track
        );
        break;
      }
      case "clip/move": {
        const params = action.params;
        const clip = this.findClip(timeline, params.clipId);
        if (clip) {
          timeline.tracks = timeline.tracks.map((track) => ({
            ...track,
            clips: track.clips.filter(
              (c) => c.id !== params.clipId
            )
          }));
          const targetTrackId = params.trackId || clip.trackId;
          timeline.tracks = timeline.tracks.map(
            (track) => track.id === targetTrackId ? {
              ...track,
              clips: [
                ...track.clips,
                {
                  ...clip,
                  startTime: params.startTime,
                  trackId: targetTrackId
                }
              ]
            } : track
          );
        }
        break;
      }
      case "clip/trim": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map((clip) => {
            if (clip.id === params.clipId) {
              const inPoint = params.inPoint ?? clip.inPoint;
              const outPoint = params.outPoint ?? clip.outPoint;
              return {
                ...clip,
                inPoint,
                outPoint,
                duration: outPoint - inPoint
              };
            }
            return clip;
          })
        }));
        break;
      }
      case "clip/split": {
        const params = action.params;
        const clip = this.findClip(timeline, params.clipId);
        if (clip) {
          const splitTime = params.time;
          const splitOffset = splitTime - clip.startTime;
          const clip1 = {
            ...clip,
            duration: splitOffset,
            outPoint: clip.inPoint + splitOffset
          };
          const clip2 = {
            ...clip,
            id: crypto.randomUUID(),
            startTime: splitTime,
            duration: clip.duration - splitOffset,
            inPoint: clip.inPoint + splitOffset
          };
          timeline.tracks = timeline.tracks.map((track) => ({
            ...track,
            clips: track.clips.flatMap(
              (c) => c.id === params.clipId ? [clip1, clip2] : [c]
            )
          }));
          this.lastAddedIds.set("clip", clip2.id);
        }
        break;
      }
      case "clip/merge": {
        const params = action.params;
        const origStart = params.originalClip.startTime;
        const origEnd = origStart + params.originalClip.duration;
        const origMediaId = params.originalClip.mediaId;
        timeline.tracks = timeline.tracks.map((track) => {
          if (track.id === params.originalClip.trackId) {
            const filteredClips = track.clips.filter((c) => {
              if (c.id === params.clipId) return false;
              if (c.mediaId === origMediaId) {
                const clipEnd = c.startTime + c.duration;
                if (c.startTime >= origStart && clipEnd <= origEnd) {
                  return false;
                }
              }
              return true;
            });
            return { ...track, clips: [...filteredClips, params.originalClip] };
          }
          return track;
        });
        break;
      }
      case "clip/rippleDelete": {
        const params = action.params;
        const clip = this.findClip(timeline, params.clipId);
        if (clip) {
          const deletedDuration = clip.duration;
          const deletedStartTime = clip.startTime;
          timeline.tracks = timeline.tracks.map((track) => {
            if (track.id === clip.trackId) {
              return {
                ...track,
                clips: track.clips.filter((c) => c.id !== params.clipId).map(
                  (c) => c.startTime > deletedStartTime ? { ...c, startTime: c.startTime - deletedDuration } : c
                )
              };
            }
            return track;
          });
        }
        break;
      }
      case "clip/setBlendMode": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? { ...clip, blendMode: params.blendMode } : clip
          )
        }));
        break;
      }
      case "clip/setBlendOpacity": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? { ...clip, blendOpacity: params.opacity } : clip
          )
        }));
        break;
      }
      case "clip/setEmphasisAnimation": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? { ...clip, emphasisAnimation: params.emphasisAnimation } : clip
          )
        }));
        break;
      }
      case "clip/setColorGrading": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? { ...clip, colorGrading: params.colorGrading } : clip
          )
        }));
        break;
      }
      case "clip/rippleRestore": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => {
          if (track.id === params.clip.trackId) {
            const restoredClips = track.clips.map((c) => {
              const affected = params.affectedClips.find(
                (ac) => ac.id === c.id
              );
              return affected ? { ...c, startTime: affected.originalStartTime } : c;
            });
            return { ...track, clips: [...restoredClips, params.clip] };
          }
          return track;
        });
        break;
      }
      case "clip/slip": {
        const params = action.params;
        const clip = this.findClip(timeline, params.clipId);
        if (clip) {
          const newInPoint = Math.max(0, clip.inPoint + params.delta);
          const newOutPoint = newInPoint + clip.duration;
          timeline.tracks = timeline.tracks.map((track) => ({
            ...track,
            clips: track.clips.map(
              (c) => c.id === params.clipId ? { ...c, inPoint: newInPoint, outPoint: newOutPoint } : c
            )
          }));
        }
        break;
      }
      case "clip/slide": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map((c) => {
            if (c.id === params.clipId) {
              return {
                ...c,
                startTime: Math.max(0, c.startTime + params.delta)
              };
            }
            if (c.id === params.prevClipId && params.delta > 0) {
              return { ...c, duration: c.duration + params.delta };
            }
            if (c.id === params.nextClipId && params.delta < 0) {
              return {
                ...c,
                startTime: c.startTime + params.delta,
                inPoint: c.inPoint - params.delta,
                duration: c.duration - params.delta
              };
            }
            return c;
          })
        }));
        break;
      }
      case "clip/roll": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map((c) => {
            if (c.id === params.leftClipId) {
              return {
                ...c,
                duration: c.duration + params.delta,
                outPoint: c.outPoint + params.delta
              };
            }
            if (c.id === params.rightClipId) {
              return {
                ...c,
                startTime: c.startTime + params.delta,
                inPoint: c.inPoint + params.delta,
                duration: c.duration - params.delta
              };
            }
            return c;
          })
        }));
        break;
      }
      case "clip/trimToPlayhead": {
        const params = action.params;
        const clip = this.findClip(timeline, params.clipId);
        if (clip) {
          timeline.tracks = timeline.tracks.map((track) => ({
            ...track,
            clips: track.clips.map((c) => {
              if (c.id !== params.clipId) return c;
              if (params.trimStart) {
                const delta = params.playheadTime - c.startTime;
                return {
                  ...c,
                  startTime: params.playheadTime,
                  inPoint: c.inPoint + delta,
                  duration: c.duration - delta
                };
              } else {
                return {
                  ...c,
                  duration: params.playheadTime - c.startTime,
                  outPoint: c.inPoint + (params.playheadTime - c.startTime)
                };
              }
            })
          }));
        }
        break;
      }
      case "clip/closeGapBefore": {
        const params = action.params;
        const clip = this.findClip(timeline, params.clipId);
        if (clip) {
          const track = timeline.tracks.find((t) => t.id === clip.trackId);
          if (track) {
            const sorted = [...track.clips].sort(
              (a, b) => a.startTime - b.startTime
            );
            const idx = sorted.findIndex((c) => c.id === clip.id);
            if (idx >= 0) {
              const prev = idx > 0 ? sorted[idx - 1] : null;
              const target = prev ? prev.startTime + prev.duration : 0;
              const gap = clip.startTime - target;
              if (gap > 0) {
                const shiftIds = new Set(
                  sorted.slice(idx).map((c) => c.id)
                );
                timeline.tracks = timeline.tracks.map((t) => {
                  if (t.id !== track.id) return t;
                  return {
                    ...t,
                    clips: t.clips.map(
                      (c) => shiftIds.has(c.id) ? { ...c, startTime: c.startTime - gap } : c
                    )
                  };
                });
              }
            }
          }
        }
        break;
      }
      case "track/consolidate": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((t) => {
          if (t.id !== params.trackId) return t;
          const sorted = [...t.clips].sort(
            (a, b) => a.startTime - b.startTime
          );
          let cursor = 0;
          const newPositions = /* @__PURE__ */ new Map();
          for (const c of sorted) {
            const newStart = Math.max(0, cursor);
            newPositions.set(c.id, newStart);
            cursor = newStart + c.duration;
          }
          return {
            ...t,
            clips: t.clips.map((c) => {
              const ns = newPositions.get(c.id);
              return ns !== void 0 && ns !== c.startTime ? { ...c, startTime: ns } : c;
            })
          };
        });
        break;
      }
      case "track/restorePositions": {
        const params = action.params;
        const lookup = new Map(
          params.positions.map((p) => [p.clipId, p.startTime])
        );
        timeline.tracks = timeline.tracks.map((t) => {
          if (t.id !== params.trackId) return t;
          return {
            ...t,
            clips: t.clips.map((c) => {
              const ns = lookup.get(c.id);
              return ns !== void 0 && ns !== c.startTime ? { ...c, startTime: ns } : c;
            })
          };
        });
        break;
      }
    }
    if (action.type === "clip/move" || action.type === "clip/trim" || action.type === "clip/slip" || action.type === "clip/trimToPlayhead") {
      const clipId = action.params.clipId;
      const updatedClip = clipId ? this.findClip(timeline, clipId) : null;
      if (updatedClip && project.nestedInstances?.some((item) => item.id === clipId)) {
        project.nestedInstances = project.nestedInstances.map(
          (item) => item.id === clipId ? {
            ...item,
            trackId: updatedClip.trackId,
            startTime: updatedClip.startTime,
            duration: updatedClip.duration,
            inPoint: updatedClip.inPoint,
            outPoint: updatedClip.outPoint,
            transform: updatedClip.transform,
            volume: updatedClip.volume
          } : item
        );
      }
    }
  }
  applyEffectAction(action, project) {
    const timeline = project.timeline;
    switch (action.type) {
      case "effect/add": {
        const params = action.params;
        const newEffect = {
          id: params.effectId ?? `effect-${crypto.randomUUID()}`,
          type: params.effectType,
          params: params.params || {},
          enabled: params.enabled ?? true
        };
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? (() => {
              const effects = [...clip.effects];
              const index = Number.isInteger(params.index) ? Math.max(0, Math.min(effects.length, params.index)) : effects.length;
              effects.splice(index, 0, newEffect);
              return { ...clip, effects };
            })() : clip
          )
        }));
        this.lastAddedIds.set("effect", newEffect.id);
        break;
      }
      case "effect/remove": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? {
              ...clip,
              effects: clip.effects.filter(
                (e) => e.id !== params.effectId
              )
            } : clip
          )
        }));
        break;
      }
      case "effect/restore": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map((clip) => {
            if (clip.id === params.clipId) {
              const effects = [...clip.effects];
              effects.splice(params.index, 0, params.effect);
              return { ...clip, effects };
            }
            return clip;
          })
        }));
        break;
      }
      case "effect/update": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? {
              ...clip,
              effects: clip.effects.map(
                (e) => e.id === params.effectId ? { ...e, params: { ...e.params, ...params.params } } : e
              )
            } : clip
          )
        }));
        break;
      }
      case "effect/toggle": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? {
              ...clip,
              effects: clip.effects.map(
                (e) => e.id === params.effectId ? { ...e, enabled: params.enabled } : e
              )
            } : clip
          )
        }));
        break;
      }
      case "effect/reorder": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map((clip) => {
            if (clip.id === params.clipId) {
              const effects = [...clip.effects];
              const effectIndex = effects.findIndex(
                (e) => e.id === params.effectId
              );
              if (effectIndex !== -1) {
                const [effect] = effects.splice(effectIndex, 1);
                effects.splice(params.newIndex, 0, effect);
              }
              return { ...clip, effects };
            }
            return clip;
          })
        }));
        break;
      }
    }
  }
  applyTransformAction(action, project) {
    const timeline = project.timeline;
    timeline.tracks = timeline.tracks.map((track) => ({
      ...track,
      clips: track.clips.map((clip) => {
        if (clip.id !== action.params.clipId) return clip;
        const prev = clip.transform;
        const next = action.params.transform;
        return {
          ...clip,
          transform: {
            ...prev,
            ...next,
            position: { ...prev.position, ...next.position ?? {} },
            scale: { ...prev.scale, ...next.scale ?? {} },
            anchor: { ...prev.anchor, ...next.anchor ?? {} }
          }
        };
      })
    }));
  }
  applyKeyframeAction(action, project) {
    const timeline = project.timeline;
    switch (action.type) {
      case "keyframe/add": {
        const params = action.params;
        const newKeyframe = {
          // Derive the id from the action so rapid sequential agent calls and
          // redo cannot collide within the same millisecond.
          id: `keyframe-${action.id}`,
          time: params.time,
          property: params.property,
          value: params.value,
          easing: params.easing ?? "linear"
        };
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? { ...clip, keyframes: [...clip.keyframes, newKeyframe] } : clip
          )
        }));
        this.lastAddedIds.set("keyframe", newKeyframe.id);
        break;
      }
      case "keyframe/remove": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? {
              ...clip,
              keyframes: clip.keyframes.filter(
                (kf) => !(kf.property === params.property && kf.time === params.time)
              )
            } : clip
          )
        }));
        break;
      }
      case "keyframe/update": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? {
              ...clip,
              keyframes: clip.keyframes.map(
                (kf) => kf.property === params.property && kf.time === params.time ? {
                  ...kf,
                  ...params.value !== void 0 && {
                    value: params.value
                  },
                  ...params.easing !== void 0 && {
                    easing: params.easing
                  }
                } : kf
              )
            } : clip
          )
        }));
        break;
      }
    }
  }
  applyTransitionAction(action, project) {
    const timeline = project.timeline;
    switch (action.type) {
      case "transition/add": {
        const params = action.params;
        const clipA = this.findClip(timeline, params.clipAId);
        if (clipA) {
          const track = timeline.tracks.find(
            (t) => t.id === clipA.trackId
          );
          if (track) {
            const newTransition = {
              id: `transition-${crypto.randomUUID()}`,
              clipAId: params.clipAId,
              clipBId: params.clipBId,
              type: params.transitionType,
              duration: params.duration,
              params: {}
            };
            track.transitions = [...track.transitions || [], newTransition];
            this.lastAddedIds.set("transition", newTransition.id);
          }
        }
        break;
      }
      case "transition/set": {
        const params = action.params;
        const clipA = this.findClip(timeline, params.transition.clipAId);
        if (clipA) {
          timeline.tracks = timeline.tracks.map(
            (track) => track.id === clipA.trackId ? {
              ...track,
              transitions: [
                ...(track.transitions || []).filter(
                  (t) => !this.isSameTransitionPlacement(t, params.transition)
                ),
                params.transition
              ]
            } : track
          );
        }
        break;
      }
      case "transition/restoreTrack": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map(
          (track) => track.id === params.trackId ? { ...track, transitions: params.transitions } : track
        );
        break;
      }
      case "transition/remove": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          transitions: (track.transitions || []).filter(
            (t) => t.id !== params.transitionId
          )
        }));
        break;
      }
      case "transition/restore": {
        const params = action.params;
        const clipA = this.findClip(timeline, params.transition.clipAId);
        if (clipA) {
          timeline.tracks = timeline.tracks.map(
            (track) => track.id === clipA.trackId ? {
              ...track,
              transitions: [
                ...track.transitions || [],
                params.transition
              ]
            } : track
          );
        }
        break;
      }
      case "transition/update": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          transitions: (track.transitions || []).map(
            (t) => t.id === params.transitionId ? {
              ...t,
              ...params.type !== void 0 && { type: params.type },
              ...params.duration !== void 0 && {
                duration: params.duration
              },
              ...params.params !== void 0 && {
                params: { ...t.params, ...params.params }
              }
            } : t
          )
        }));
        break;
      }
    }
  }
  applyAudioAction(action, project) {
    const timeline = project.timeline;
    switch (action.type) {
      case "audio/setVolume": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? { ...clip, volume: params.volume } : clip
          )
        }));
        break;
      }
      case "audio/setFade": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? {
              ...clip,
              fade: {
                fadeIn: params.fadeIn !== void 0 ? params.fadeIn : clip.fade?.fadeIn || 0,
                fadeOut: params.fadeOut !== void 0 ? params.fadeOut : clip.fade?.fadeOut || 0
              }
            } : clip
          )
        }));
        break;
      }
      case "audio/addAutomation": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? {
              ...clip,
              automation: {
                ...clip.automation,
                volume: params.points
              }
            } : clip
          )
        }));
        break;
      }
      case "audio/addEffect": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? {
              ...clip,
              audioEffects: [...clip.audioEffects ?? [], params.effect]
            } : clip
          )
        }));
        break;
      }
      case "audio/restoreEffect": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map((clip) => {
            if (clip.id !== params.clipId) return clip;
            const audioEffects = [...clip.audioEffects ?? []];
            audioEffects.splice(params.index, 0, params.effect);
            return { ...clip, audioEffects };
          })
        }));
        break;
      }
      case "audio/removeEffect": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? {
              ...clip,
              audioEffects: (clip.audioEffects ?? []).filter(
                (e) => e.id !== params.effectId
              )
            } : clip
          )
        }));
        break;
      }
      case "audio/updateEffect": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? {
              ...clip,
              audioEffects: (clip.audioEffects ?? []).map(
                (e) => e.id === params.effectId ? { ...e, params: { ...e.params, ...params.params } } : e
              )
            } : clip
          )
        }));
        break;
      }
      case "audio/toggleEffect": {
        const params = action.params;
        timeline.tracks = timeline.tracks.map((track) => ({
          ...track,
          clips: track.clips.map(
            (clip) => clip.id === params.clipId ? {
              ...clip,
              audioEffects: (clip.audioEffects ?? []).map(
                (e) => e.id === params.effectId ? { ...e, enabled: params.enabled } : e
              )
            } : clip
          )
        }));
        break;
      }
    }
  }
  applySubtitleAction(action, project) {
    const timeline = project.timeline;
    switch (action.type) {
      case "subtitle/add": {
        const params = action.params;
        const newSubtitle = {
          id: `subtitle-${crypto.randomUUID()}`,
          text: params.text,
          startTime: params.startTime,
          endTime: params.endTime
        };
        timeline.subtitles = [...timeline.subtitles || [], newSubtitle];
        this.lastAddedIds.set("subtitle", newSubtitle.id);
        break;
      }
      case "subtitle/remove": {
        const params = action.params;
        timeline.subtitles = (timeline.subtitles || []).filter(
          (s) => s.id !== params.subtitleId
        );
        break;
      }
      case "subtitle/restore": {
        const params = action.params;
        timeline.subtitles = [...timeline.subtitles || [], params.subtitle];
        break;
      }
      case "subtitle/restoreAll": {
        const params = action.params;
        timeline.subtitles = params.subtitles;
        break;
      }
      case "subtitle/update": {
        const params = action.params;
        timeline.subtitles = (timeline.subtitles || []).map(
          (s) => s.id === params.subtitleId ? {
            ...s,
            ...params.text !== void 0 && { text: params.text },
            ...params.startTime !== void 0 && {
              startTime: params.startTime
            },
            ...params.endTime !== void 0 && {
              endTime: params.endTime
            }
          } : s
        );
        break;
      }
      case "subtitle/setStyle": {
        const params = action.params;
        timeline.subtitles = (timeline.subtitles || []).map((s) => ({
          ...s,
          style: params.style
        }));
        break;
      }
      case "subtitle/import": {
        const params = action.params;
        const srtRegex = /(\d+)\n(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n([\s\S]*?)(?=\n\n|\n*$)/g;
        let match;
        const newSubtitles = [];
        while ((match = srtRegex.exec(params.srtContent)) !== null) {
          const startTime = this.parseSrtTime(match[2]);
          const endTime = this.parseSrtTime(match[3]);
          const text = match[4].trim();
          newSubtitles.push({
            id: `subtitle-${Date.now()}-${match[1]}`,
            text,
            startTime,
            endTime
          });
        }
        if (newSubtitles.length > 0) {
          timeline.subtitles = [...timeline.subtitles || [], ...newSubtitles];
        }
        break;
      }
    }
  }
  parseSrtTime(timeString) {
    const [time, ms] = timeString.split(",");
    const [hours, minutes, seconds] = time.split(":").map(Number);
    return hours * 3600 + minutes * 60 + seconds + Number(ms) / 1e3;
  }
  findClip(timeline, clipId) {
    for (const track of timeline.tracks) {
      const clip = track.clips.find((c) => c.id === clipId);
      if (clip) return clip;
    }
    return null;
  }
};
export {
  ActionExecutor,
  ActionHistory
};
