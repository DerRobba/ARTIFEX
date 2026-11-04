// ARTIFEX template: wall hook (printed lying on its side for strength)
width = 20;
plate_h = 60;
plate_t = 4;
hook_depth = 30;
hook_t = 5;
lip_h = 14;
screw_d = 4.5;
$fn = 48;

module profile() {
    square([plate_t, plate_h]);
    square([hook_depth + hook_t, hook_t]);
    translate([hook_depth, 0]) square([hook_t, lip_h]);
    // fillet between plate and arm
    translate([plate_t, hook_t]) difference() {
        square([4, 4]);
        translate([4, 4]) circle(r = 4);
    }
}

translate([-(hook_depth + hook_t) / 2, -plate_h / 2, 0]) difference() {
    linear_extrude(width) profile();
    for (y = [plate_h * 0.5, plate_h * 0.85])
        translate([-1, y, width / 2]) rotate([0, 90, 0]) cylinder(d = screw_d, h = plate_t + 2);
}
