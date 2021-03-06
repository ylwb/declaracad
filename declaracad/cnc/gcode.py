"""
Copyright (c) 2020, Jairus Martin.

Distributed under the terms of the GPL v3 License.

The full license is in the file LICENSE, distributed with this software.

Created on Aug 18, 2020

@author: jrm
"""
import os
import re
from collections import OrderedDict
from atom.api import (
    Atom, Property, List, Instance, Str, Int, Enum, Float, Bool, Dict, observe
)
from OCCT.BRep import BRep_Builder
from OCCT.TopoDS import TopoDS_Compound
from declaracad.occ.api import Point


def normalize(k, v):
    """ Normalize an ID command

    """
    vi = int(v)
    if v == vi:
        v = vi  # Clip off .0
    return '{}{}'.format(k, v)



class Command(Atom):
    data = Instance(OrderedDict)
    id = Str()
    comment = Str()
    source = Str()
    line = Int()

    def _default_id(self):
        if self.data:
            for k, v in self.data.items():
                if k in GCode.ID_CODES:
                    return normalize(k, v)
        return ''

    def _get_waypoint(self):
        d = self.data
        if not d:
            return
        axis = {}
        for k in GCode.AXIS_CODES:
            if k in d:
                axis[k] = d[k]
        if axis:
            return Waypoint(**axis)

    waypoint = Property(_get_waypoint, cached=True)

    def position(self, last):
        """ Get the 3D-position of this cmd's XYZ coordinates.

        Parameters
        ----------
        last: Point
            The previous position.

        Returns
        -------
        point: Point
            The position of this command

        """
        data = self.data
        x = data.get('X')
        y = data.get('Y')
        z = data.get('Z')
        return Point(
                last.x if x is None else x,
                last.y if y is None else y,
                last.z if z is None else z)

    def _get_feedrate(self):
        if not self.data:
            return
        return self.data.get('F')

    feedrate = Property(_get_feedrate, cached=True)

    def _get_is_move(self):
        return self.id in GCode.MOVE_CODES

    is_move = Property(_get_is_move, cached=True)

    def __repr__(self):
        return "Command<{} from '{}' at line {}>".format(
            self.id, self.source, self.line)


class GCode(Atom):
    path = Str()
    commands = List(Command)

    AXIS_CODES = 'XYZABCUVW'
    ID_CODES = 'GMTS'
    MOVE_CODES = ('G0', 'G1', 'G2', 'G3', 'G5', 'G5.1')

    COLORMAP = {
        'G0': 'green',
        'G1': 'blue',
        'G2': 'green',
        'G3': 'green',
    }

    def __repr__(self):
        return "GCode<file='{} cmds=[\n    {}\n]>".format(
            self.path, ",\n    ".join(map(str, self.commands[0:100])))

    def max(self):
        """ Return max value of each axis """
        return Point(*(max(c.data[axis] for c in self.commands
                          if c.data and axis in c.data)
                     for axis in ('X', 'Y', 'Z')))

    def min(self):
        """ Return min value of each axis """
        return Point(*(min(c.data[axis] for c in self.commands
                          if c.data and axis in c.data)
                     for axis in ('X', 'Y', 'Z')))


class Movement(Atom):
    rapid = Bool()
    points = List()

    def clone(self):
        return Movement(
            rapid=self.rapid,
            points=[Point(*p) for p in self.points])


def convert(v, scale=1, precision=None):
    """ Convert a value for writing to gcode

    Parameters
    ----------
    v: Float
        The value to convert
    scale: Float
        The scale to apply
    precision: None or Int
        The precision to apply, if 0 convert to integer, if None
        use full precision, otherwise round to given decimal places.

    Returns
    -------
    v: Int or Float
        The converted value

    """
    if precision == 0:
        return int(v*scale)
    elif precision is None:
        return v*scale
    return round(v*scale, precision)


def save_to_file(filename, movements, device):
    """ Write to a file

    Parameters
    ----------
    filename: String
        The path to the file to save
    movements: List[Movement]
        List of movements to save
    device: Device
        Device to save it for
    """
    with open(filename, 'w') as f:
        f.write("(Generated by DeclaraCAD)\n")
        f.write(f"(Path: {filename})\n")
        if device.config.init_commands:
            f.write(device.config.init_commands)
        for movement in movements:
            cmd = "G0" if movement.rapid else "G1"
            for point in movement.points:
                x, y, z = device.convert(point)
                f.write(f"{cmd} X{x} Y{y} Z{z}\n")
        if device.config.finalize_commands:
            f.write(device.config.finalize_commands)


def parse(path):
    """ Parse the file at the given path into a list of Commands

    Parameters
    ----------
    path: String
        The file path

    Notes
    -----
    This does not handle inline comments or multiple commands on a single line

    Returns
    -------
    gcode: GCode
        A GCode instance with the parsed commands

    """
    cmds = []

    def set_id(cmd):
        if not cmd.id:
            # If command is not specified use the last move
            for c in reversed(cmds):
                if c.is_move:
                    cmd.id = c.id
                    break
        return cmd.id

    def finish(cmd):
        set_id(cmd)
        cmds.append(cmd)

    with open(path) as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue

            # Strip comments
            parts = re.split(r';|\(|%', line, maxsplit=1)
            data = parts[0].strip()
            comment = "" if len(parts) == 1 else parts[1]
            if not data and not comment:
                continue

            cmd = Command(comment=comment, source=line, line=i+1)
            if not data:
                cmds.append(cmd)  # Comment
                continue

            try:
                # Parse args
                args = []
                for c in re.findall(r'[A-z] *-?[\d.]+ *', data):
                    args.append((c[0].upper(), float(c[1:])))

                keys = set((it[0] for it in args))

                # Since some files put mode changes on the same line
                # split them into separate commands
                cmd.data = d = OrderedDict()
                for k, v in args:
                    if k in d:
                        # HACK: Split out to a new command
                        # when duplicate keys are given in the same line, eg:
                        #     N40 G90 G00 X0 Y0
                        # is split into a G90 and G0
                        finish(cmd)
                        cmd = Command(comment=comment,
                                      source=line,
                                      line=i+1)
                        cmd.data = d = OrderedDict()
                    elif k in GCode.AXIS_CODES:
                        # HACK: If we get move arguments for a non-move split
                        # the command, eg a
                        #    N100 G01 X30 Y50
                        #    N110 G91 X10.1 Y-10.1
                        # should be split into a G1, G91, G1
                        cmd_id = set_id(cmd)
                        if cmd_id not in GCode.MOVE_CODES:
                            finish(cmd)
                            cmd = Command(comment=comment,
                                          source=line,
                                          line=i+1)
                            cmd.data = d = OrderedDict()
                    d[k] = v
                finish(cmd)
            except ValueError as e:
                filepath, filename = os.path.split(path)
                msg = "Failed to parse '%s' at line %s: %s" % (
                    filename, i, e)
                raise ValueError(msg)

    return GCode(path=path, commands=cmds)


class Waypoint(Atom):
    X = Float()
    Y = Float()
    Z = Float()
    A = Float()
    B = Float()
    C = Float()
    U = Float()
    V = Float()
    W = Float()


