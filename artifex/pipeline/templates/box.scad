// ARTIFEX template: open box / container with rounded corners
width = 60;
depth = 40;
height = 30;
wall = 2;
floor_t = 1.6;
corner_r = 3;
$fn = 48;

module rounded_block(w, d, h, r) {
    rr = max(min(r, min(w, d) / 2 - 0.01), 0.01);
    linear_extrude(h) offset(r = rr) square([w - 2 * rr, d - 2 * rr], center = true);
}

difference() {
    rounded_block(width, depth, height, corner_r);
    translate([0, 0, floor_t])
        rounded_block(width - 2 * wall, depth - 2 * wall, height, max(corner_r - wall, 0.5));
}
