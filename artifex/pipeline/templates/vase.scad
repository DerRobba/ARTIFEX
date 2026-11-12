// ARTIFEX template: twisted polygon vase
diameter = 70;
height = 120;
sides = 6;
twist = 90;
top_scale = 1.3;
wall = 1.6;
floor_t = 2;

s0 = 1 + (top_scale - 1) * floor_t / height;

difference() {
    linear_extrude(height, twist = twist, scale = top_scale, slices = 60)
        circle(d = diameter, $fn = sides);
    translate([0, 0, floor_t]) rotate([0, 0, -twist * floor_t / height])
        linear_extrude(height - floor_t + 0.5, twist = twist * (height - floor_t + 0.5) / height,
                       scale = top_scale / s0, slices = 60)
            scale(s0) offset(r = -wall) circle(d = diameter, $fn = sides);
}
