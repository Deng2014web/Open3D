#!/usr/bin/env python

###############################################################################
# The MIT License (MIT)
#
# Open3D: www.open3d.org
# Copyright (c) 2020 www.open3d.org
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
###############################################################################
"""Online 3D depth video processing pipeline.

- Connects to a RGBD camera or RGBD video file (currently
  RealSense camera and bag file format are supported).
- Captures / reads color and depth frames. Allow recording from camera.
- Convert frames to point cloud, optionally with normals.
- Visualize point cloud video and results.
- Save point clouds for selected frames.
"""

import json
import time
import logging as log
import argparse
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import open3d as o3d
import open3d.visualization.gui as gui
import open3d.visualization.rendering as rendering

import gtimer as gt


# camera and processing
class PipelineModel:

    def __init__(self,
                 update_view,
                 camera_config_file=None,
                 rgbd_video=None,
                 device=None):
        self.update_view = update_view
        if device:
            self.device = device.lower()
        else:
            self.device = 'cuda:0' if o3d.core.cuda.is_available() else 'cpu:0'
        self.o3d_device = o3d.core.Device(self.device)

        self.video = None
        self.camera = None
        self.flag_capture = False
        self.cv_capture = threading.Condition()
        self.recording = False  # Are we currently recording
        self.flag_record = False  # Request to start/stop recording
        if rgbd_video:  # Video file
            self.video = o3d.t.io.RGBDVideoReader.create(rgbd_video)
            self.rgbd_metadata = self.video.metadata

        else:  # Depth camera
            now = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            filename = f"{now}.bag"
            self.camera = o3d.t.io.RealSenseSensor()
            if camera_config_file:
                with open(camera_config_file) as ccf:
                    self.camera.init_sensor(o3d.t.io.RealSenseSensorConfig(
                        json.load(ccf)),
                                            filename=filename)
            else:
                self.camera.init_sensor(filename=filename)

            self.camera.start_capture(start_record=False)
            self.rgbd_metadata = self.camera.get_metadata()

        self.max_points = self.rgbd_metadata.width * self.rgbd_metadata.height
        log.info(self.rgbd_metadata)

        # RGBD -> PCD
        self.extrinsics = o3d.core.Tensor.eye(4, dtype=o3d.core.Dtype.Float32)
        self.intrinsic_matrix = o3d.core.Tensor(
            self.rgbd_metadata.intrinsics.intrinsic_matrix,
            dtype=o3d.core.Dtype.Float32)
        self.calib = {
            'world_cam':
                self.extrinsics.numpy().astype(np.float32),
            'cam_img':
                np.block([[self.intrinsic_matrix.numpy(),
                           np.zeros((3, 1))], [np.zeros((1, 3)),
                                               1]]).astype(np.float32).T
        }
        self.depth_max = 3.0  # m
        self.pcd_stride = 1  # downsample point cloud
        self.flag_normals = False

        self.vfov = 1.25 * np.rad2deg(
            2 * np.arctan(self.intrinsic_matrix[1, 2].item() /
                          self.intrinsic_matrix[1, 1].item()))
        self.pcd_frame = None
        self.rgbd_frame = None
        self.executor = ThreadPoolExecutor(max_workers=3,
                                           thread_name_prefix='Capture-Save')
        self.flag_exit = False

    def run(self):
        """Run pipeline"""
        n_pts = 0
        frame_id = 0
        t1 = time.perf_counter()
        gt.stamp('Startup')
        if self.video:
            self.rgbd_frame = self.video.next_frame()
        else:
            self.rgbd_frame = self.camera.capture_frame(
                wait=True, align_depth_to_color=True)

        pcd_errors = 0
        while (frame_id < 1000 and not self.flag_exit and
               (self.video is None or self.video and not self.video.is_eof())):
            if self.video:
                future_rgbd_frame = self.executor.submit(self.video.next_frame)
            else:
                future_rgbd_frame = self.executor.submit(
                    self.camera.capture_frame,
                    wait=True,
                    align_depth_to_color=True)
            gt.stamp("SubmitCapture", unique=False)

            try:
                self.rgbd_frame = self.rgbd_frame.to(self.o3d_device)
                self.pcd_frame = o3d.t.geometry.PointCloud.create_from_rgbd_image(
                    self.rgbd_frame, self.intrinsic_matrix, self.extrinsics,
                    self.rgbd_metadata.depth_scale, self.depth_max,
                    self.pcd_stride, self.flag_normals)
                gt.stamp("DepthToPCD", unique=False)
                depth_in_color = self.rgbd_frame.depth.colorize_depth(
                    self.rgbd_metadata.depth_scale, 0, self.depth_max)
                gt.stamp("ColorizeDepth", unique=False)
            except RuntimeError:
                pcd_errors += 1

            n_pts += self.pcd_frame.point['points'].shape[0]
            frame_elements = {
                'color': self.rgbd_frame.color,
                'depth': depth_in_color,
                'pcd': self.pcd_frame
            }
            if self.pcd_frame.is_empty():
                log.warning(f"No valid depth data in frame {frame_id})")
                continue

            self.update_view(frame_elements)
            self.rgbd_frame = future_rgbd_frame.result()
            gt.stamp("GetCapture", unique=False)
            if frame_id % 30 == 0:
                t0, t1 = t1, time.perf_counter()
                # frame_elements['status_message'] = f"FPS: {30./(t1-t0):0.2f}"
                log.debug(f"\nframe_id = {frame_id}, \t {(t1-t0)*1000./30:0.2f}"
                          f"ms/frame \t {(t1-t0)*1e9/n_pts} ms/Mp\t")
                n_pts = 0

            with self.cv_capture:  # Wait for capture to be enabled
                self.cv_capture.wait_for(
                    predicate=lambda: self.flag_capture or self.flag_exit)
            self.toggle_record()
            gt.stamp("UserWait", unique=False)
            frame_id += 1

        if self.camera:
            self.camera.stop_capture()
        else:
            self.video.close()
        self.executor.shutdown()
        log.debug(f"create_from_depth_image() errors = {pcd_errors}")
        log.debug(gt.report())

    def toggle_record(self):
        if self.camera is not None:
            if self.flag_record and not self.recording:
                self.camera.resume_record()
                self.recording = True
            elif not self.flag_record and self.recording:
                self.camera.pause_record()
                self.recording = False

    def on_save_pcd(self):
        """Callback to save current point cloud."""
        now = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        filename = f"{self.rgbd_metadata.serial_number}_pcd_{now}.ply"
        # Convert colors to uint8 for compatibility
        self.pcd_frame.point['colors'] = (self.pcd_frame.point['colors'] *
                                          255).to(o3d.core.Dtype.UInt8)
        self.executor.submit(o3d.t.io.write_point_cloud,
                             filename,
                             self.pcd_frame,
                             write_ascii=False,
                             compressed=True,
                             print_progress=False)

    def on_save_rgbd(self):
        """Callback to save current RGBD image pair."""
        now = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        filename = f"{self.rgbd_metadata.serial_number}_color_{now}.jpg"
        self.executor.submit(o3d.t.io.write_image, filename,
                             self.rgbd_frame.color)
        filename = f"{self.rgbd_metadata.serial_number}_depth_{now}.png"
        self.executor.submit(o3d.t.io.write_image, filename,
                             self.rgbd_frame.depth)


# Main thread, GUI scene
class PipelineView:

    def __init__(self, vfov=60, max_pcd_vertices=1 << 20, **callbacks):

        self.vfov = vfov
        self.max_pcd_vertices = max_pcd_vertices

        gui.Application.instance.initialize()
        self.window = gui.Application.instance.create_window(
            "Open3D || Online Depth Video Processing", 1024, 768)
        # Called on window layout (eg: resize)
        self.window.set_on_layout(self.on_layout)
        self.window.set_on_close(callbacks['on_window_close'])

        self.pcd_material = o3d.visualization.rendering.Material()
        self.pcd_material.shader = "defaultLit"
        # Set n_pixels displayed for each 3D point, accounting for HiDPI scaling
        self.pcd_material.point_size = 8 * self.window.scaling

        # 3D scene
        self.pcdview = gui.SceneWidget()
        self.window.add_child(self.pcdview)
        self.pcdview.enable_scene_caching(
            True)  # makes UI _much_ more responsive
        self.pcdview.scene = rendering.Open3DScene(self.window.renderer)
        self.pcdview.scene.set_background([1, 1, 1, 1])  # White background
        self.pcdview.scene.set_lighting(
            rendering.Open3DScene.LightingProfile.SOFT_SHADOWS, [0, -6, 0])
        self.pcdview.scene.show_axes(True)
        # Point cloud bounds, depends on the sensor range
        self.pcd_bounds = o3d.geometry.AxisAlignedBoundingBox([-3, -3, 0],
                                                              [3, 3, 6])
        self.camera_view()  # Initially look from the camera
        em = self.window.theme.font_size

        # Options panel
        self.panel = gui.Vert(em / 2, gui.Margins(em, em, em, em))
        self.window.add_child(self.panel)
        toggles = gui.Horiz(2 * em)
        self.panel.add_child(toggles)

        toggle_capture = gui.ToggleSwitch("Capture / Play")
        toggle_capture.is_on = False
        toggle_capture.set_on_clicked(
            callbacks['on_toggle_capture'])  # callback
        toggles.add_child(toggle_capture)

        self.flag_normals = False
        self.toggle_normals = gui.ToggleSwitch("Colors / Normals")
        self.toggle_normals.is_on = False
        self.toggle_normals.set_on_clicked(
            callbacks['on_toggle_normals'])  # callback
        toggles.add_child(self.toggle_normals)

        view_buttons = gui.Horiz(2 * em)
        self.panel.add_child(view_buttons)
        camera_view = gui.Button("Camera view")
        camera_view.set_on_clicked(self.camera_view)  # callback
        view_buttons.add_child(camera_view)
        birds_eye_view = gui.Button("Bird's eye view")
        birds_eye_view.set_on_clicked(self.birds_eye_view)  # callback
        view_buttons.add_child(birds_eye_view)
        self.panel.add_fixed(em)  # spacing

        save_buttons = gui.Horiz(2 * em)
        self.panel.add_child(save_buttons)
        self.toggle_record = None
        if callbacks['on_toggle_record'] is not None:
            self.toggle_record = gui.ToggleSwitch("Record video")
            self.toggle_record.is_on = False
            self.toggle_record.set_on_clicked(callbacks['on_toggle_record'])
            save_buttons.add_child(self.toggle_record)

        save_pcd = gui.Button("Save point cloud")
        save_pcd.set_on_clicked(callbacks['on_save_pcd'])
        save_buttons.add_child(save_pcd)
        save_rgbd = gui.Button("Save RGBD frame")
        save_rgbd.set_on_clicked(callbacks['on_save_rgbd'])
        save_buttons.add_child(save_rgbd)
        self.panel.add_fixed(em)  # spacing

        video_size = (320, 480, 3)
        self.constraints = gui.Widget.Constraints()
        self.constraints.width = video_size[1]
        self.constraints.height = video_size[0]
        self.show_color = gui.CollapsableVert("Color image")
        self.panel.add_child(self.show_color)
        self.color_video = gui.ImageWidget(
            o3d.geometry.Image(np.zeros(video_size, dtype=np.uint8)))
        self.show_color.add_child(self.color_video)
        self.show_depth = gui.CollapsableVert("Depth image")
        self.panel.add_child(self.show_depth)
        self.depth_video = gui.ImageWidget(
            o3d.geometry.Image(np.zeros(video_size, dtype=np.uint8)))
        self.show_depth.add_child(self.depth_video)

        self.panel.add_fixed(em)  # spacing
        self.status_message = gui.Label("")
        self.panel.add_child(self.status_message)

        self.flag_exit = False
        self.flag_gui_init = False

    def update(self, frame_elements):
        """Update visualization with point cloud and bounding boxes
        Must run in main thread since this makes GUI calls.

        Args:
            frame_elements: dict {element_type: geometry element}.
                Dictionary of element types to geometry elements to be updated
                in the GUI:
                    'pcd': point cloud,
                    'color': rgb image (3 channel, uint8),
                    'depth': depth image (uint8),
                    'status_message': message
        """
        if not self.flag_gui_init:
            # Set dummy point cloud to allocate graphics memory
            dummy_pcd = o3d.t.geometry.PointCloud({
                'points':
                    o3d.core.Tensor.zeros((self.max_pcd_vertices, 3),
                                          o3d.core.Dtype.Float32),
                'colors':
                    o3d.core.Tensor.zeros((self.max_pcd_vertices, 3),
                                          o3d.core.Dtype.Float32),
                'normals':
                    o3d.core.Tensor.zeros((self.max_pcd_vertices, 3),
                                          o3d.core.Dtype.Float32)
            })
            if self.pcdview.scene.has_geometry('pcd'):
                self.pcdview.scene.remove_geometry('pcd')

            self.pcd_material.shader = "normals" if self.flag_normals else "defaultLit"
            self.pcdview.scene.add_geometry('pcd', dummy_pcd, self.pcd_material)
            self.flag_gui_init = True

        update_flags = (
            rendering.Scene.UPDATE_POINTS_FLAG |
            rendering.Scene.UPDATE_COLORS_FLAG |
            (rendering.Scene.UPDATE_NORMALS_FLAG if self.flag_normals else 0))
        self.pcdview.scene.scene.update_geometry('pcd',
                                                 frame_elements['pcd'].cpu(),
                                                 update_flags)

        # Update color and depth images
        if self.show_color.get_is_open() and 'color' in frame_elements:
            self.color_video.update_image(frame_elements['color'].cpu())
            # to(o3d.core.Device('cpu:0'), copy=True))
        if self.show_depth.get_is_open() and 'depth' in frame_elements:
            self.depth_video.update_image(frame_elements['depth'].cpu())

        if 'status_message' in frame_elements:
            self.status_message.text = frame_elements["status_message"]

        self.pcdview.force_redraw()
        gt.stamp(name="GUI", unique=False)

    def camera_view(self):
        """Callback to reset point cloud view to the camera"""
        self.pcdview.setup_camera(self.vfov, self.pcd_bounds, [0, 0, 0])
        # Look at [0, 0, 1] from camera placed at [0, 0, 0] with Y axis
        # pointing at [0, -1, 0]
        self.pcdview.scene.camera.look_at([0, 0, 1], [0, 0, 0], [0, -1, 0])

    def birds_eye_view(self):
        """Callback to reset point cloud view to birds eye (overhead) view"""
        self.pcdview.setup_camera(self.vfov, self.pcd_bounds, [0, 0, 0])
        self.pcdview.scene.camera.look_at([0, 0, 1.5], [0, 3, 1.5], [0, -1, 0])

    def on_layout(self, layout_context):
        """Callback on window initialize / resize"""
        frame = self.window.content_rect
        em = layout_context.theme.font_size
        panel_size = self.panel.calc_preferred_size(layout_context,
                                                    self.constraints)
        panel_rect = gui.Rect(frame.get_right() - panel_size.width - em,
                              frame.y + em, panel_size.width, panel_size.height)
        self.panel.frame = panel_rect
        self.pcdview.frame = frame


# Main thread, GUI options panel
class PipelineController:

    def __init__(self, camera_config_file=None, rgbd_video=None, device=None):
        self.pipeline_model = PipelineModel(self.update_view,
                                            camera_config_file, rgbd_video,
                                            device)

        self.pipeline_view = PipelineView(
            on_window_close=self.on_window_close,
            on_toggle_capture=self.on_toggle_capture,
            on_save_pcd=self.pipeline_model.on_save_pcd,
            on_save_rgbd=self.pipeline_model.on_save_rgbd,
            on_toggle_record=self.on_toggle_record
            if rgbd_video is None else None,
            on_toggle_normals=self.on_toggle_normals)

        threading.Thread(name='PipelineModel',
                         target=self.pipeline_model.run).start()
        gui.Application.instance.run()

    def update_view(self, frame_elements):
        """May be called from any thread."""
        gui.Application.instance.post_to_main_thread(
            self.pipeline_view.window,
            lambda: self.pipeline_view.update(frame_elements))

    def on_toggle_capture(self, is_enabled):
        """Callback to toggle capture."""
        self.pipeline_model.flag_capture = is_enabled
        if not is_enabled:
            self.on_toggle_record(False)
            if self.pipeline_view.toggle_record is not None:
                self.pipeline_view.toggle_record.is_on = False
        else:
            with self.pipeline_model.cv_capture:
                self.pipeline_model.cv_capture.notify()

    def on_toggle_record(self, is_enabled):
        """Callback to toggle recording RGBD video."""
        self.pipeline_model.flag_record = is_enabled

    def on_toggle_normals(self, is_enabled):
        """Callback to toggle display of normals"""
        self.pipeline_model.flag_normals = is_enabled
        self.pipeline_view.flag_normals = is_enabled
        self.pipeline_view.flag_gui_init = False

    def on_window_close(self):
        """Callback when the user closes the application window"""
        self.pipeline_model.flag_exit = True
        with self.pipeline_model.cv_capture:
            self.pipeline_model.cv_capture.notify_all()
        return True  # OK to close window


if __name__ == "__main__":

    log.basicConfig(level=log.DEBUG)
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--camera-config',
                        help='RGBD camera configuration JSON file')
    parser.add_argument('--rgbd-video', help='RGBD video file (RealSense bag)')
    parser.add_argument('--device',
                        help='Device to run computations. e.g. cpu:0 or cuda:0 '
                        'Default is CUDA GPU if available, else CPU.')

    args = parser.parse_args()
    if args.camera_config and args.rgbd_video:
        log.critical(
            "Please provide only one of --camera-config and --rgbd-video arguments"
        )
    else:
        PipelineController(args.camera_config, args.rgbd_video, args.device)
