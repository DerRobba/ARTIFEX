// ARTIFEX template: phone / tablet stand (printed lying on its side)
width = 70;
base_depth = 90;
angle = 65;
thickness = 5;
lip_h = 14;
device_gap = 13;
$fn = 48;

rest_x = base_depth * 0.3;
rest_len = base_depth * 1.05;
lip_x = max(rest_x - device_gap / sin(angle) - thickness, 0);

module profile() {
    square([base_depth, thickness]);
    translate([lip_x, 0]) square([thickness, lip_h]);
    translate([rest_x, 0]) rotate(angle) square([rest_len, thickness]);
    hull() {
        translate([base_depth - thickness, 0]) square([thickness, thickness]);
        translate([rest_x, 0]) rotate(angle) translate([rest_len * 0.55, 0]) square([thickness, thickness]);
    }
}

translate([-base_depth / 2, -base_depth / 2, 0]) linear_extrude(width) profile();
