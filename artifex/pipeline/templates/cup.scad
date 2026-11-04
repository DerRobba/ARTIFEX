// ARTIFEX template: cup / pen holder
diameter = 70;
height = 90;
wall = 2;
floor_t = 2;
chamfer = 1;
$fn = 96;

difference() {
    union() {
        cylinder(d1 = diameter - 2 * chamfer, d2 = diameter, h = chamfer);
        translate([0, 0, chamfer]) cylinder(d = diameter, h = height - chamfer);
    }
    translate([0, 0, floor_t]) cylinder(d = diameter - 2 * wall, h = height);
}
