// ARTIFEX template: sign / name plate / key tag with raised text
label = "ARTIFEX";
width = 100;
height = 30;
thickness = 3;
text_height = 1.2;
text_size = 0;      // 0 = automatic
corner_r = 4;
hole_d = 0;         // > 0 adds a hole on the left (key tag)
$fn = 48;

hole_space = hole_d > 0 ? hole_d + 4 : 0;
auto_size = min(height * 0.55, (width - hole_space - 6) / max(len(label), 1) * 1.35);
ts = text_size > 0 ? text_size : auto_size;

union() {
    difference() {
        linear_extrude(thickness)
            offset(r = corner_r) square([width - 2 * corner_r, height - 2 * corner_r], center = true);
        if (hole_d > 0)
            translate([-width / 2 + hole_d / 2 + 3, 0, -1]) cylinder(d = hole_d, h = thickness + 2);
    }
    translate([hole_space / 2, 0, thickness])
        linear_extrude(text_height)
            text(label, size = ts, font = "Liberation Sans:style=Bold", halign = "center", valign = "center");
}
