"""
Last modified date: 2022.03.11
Author: mzhmxzh
Description: initializations
"""

import os
import torch
import transforms3d
import math
import pytorch3d.structures
import pytorch3d.ops
import trimesh as tm
import numpy as np
import torch.nn.functional


def initialize_table_top(hand_model, object_model, args):
    device = hand_model.device
    n_objects = len(object_model.object_mesh_list)
    batch_size_each = object_model.batch_size_each
    total_batch_size = n_objects * batch_size_each

    # sample table plane

    object_model.plane_parameters = torch.zeros([total_batch_size, 4], dtype=torch.float, device=device)
    for i in range(n_objects):
        pose_matrices = np.load(os.path.join(args.poses, object_model.object_code_list[i] + '.npy'))
        pose_matrices = pose_matrices[np.random.random_integers(0, len(pose_matrices) - 1, batch_size_each)]
        object_model.plane_parameters[i * batch_size_each : (i + 1) * batch_size_each] = torch.tensor(pose_matrices[:, 2], dtype=torch.float, device=device)
        object_model.plane_parameters[i * batch_size_each : (i + 1) * batch_size_each, 3] *= object_model.object_scale_tensor[i]  # (A, B, C, D) = (R_31, R_32, R_33, T_3 * object_scale)

    # sample hand pose

    translation = torch.zeros([total_batch_size, 3], dtype=torch.float, device=device)
    rotation = torch.zeros([total_batch_size, 3, 3], dtype=torch.float, device=device)
    mask_solved = torch.zeros([total_batch_size], dtype=torch.bool, device=device)

    for i in range(n_objects):
        
        # get inflated convex hull

        mesh_origin = object_model.object_mesh_list[i].convex_hull
        vertices = mesh_origin.vertices.copy()
        faces = mesh_origin.faces
        vertices *= object_model.object_scale_tensor[i].max().item()
        mesh_origin = tm.Trimesh(vertices, faces)
        mesh_origin.faces = mesh_origin.faces[mesh_origin.remove_degenerate_faces()]
        vertices += 0.2 * vertices / np.linalg.norm(vertices, axis=1, keepdims=True)
        mesh = tm.Trimesh(vertices=vertices, faces=faces).convex_hull
        vertices = torch.tensor(mesh.vertices, dtype=torch.float, device=device)
        faces = torch.tensor(mesh.faces, dtype=torch.float, device=device)
        mesh_pytorch3d = pytorch3d.structures.Meshes(vertices.unsqueeze(0), faces.unsqueeze(0))

        # sample dense pc on inflated convex hull

        dense_point_cloud = pytorch3d.ops.sample_points_from_meshes(mesh_pytorch3d, num_samples=100 * batch_size_each)

        while not mask_solved[i * batch_size_each : (i + 1) * batch_size_each].all():

            unsolved_indices = (~mask_solved[i * batch_size_each : (i + 1) * batch_size_each]).nonzero().reshape(-1) + i * batch_size_each
            n_unsolved = len(unsolved_indices)

            # sample anchor points from dence pc

            p = pytorch3d.ops.sample_farthest_points(dense_point_cloud, K=n_unsolved, random_start_point=True)[0][0]
            closest_points, _, _ = mesh_origin.nearest.on_surface(p.detach().cpu().numpy())
            closest_points = torch.tensor(closest_points, dtype=torch.float, device=device)
            n = (closest_points - p) / (closest_points - p).norm(dim=1).unsqueeze(1)

            # sample parameters

            distance = args.distance_lower + (args.distance_upper - args.distance_lower) * torch.rand([n_unsolved], dtype=torch.float, device=device)
            deviate_theta = args.theta_lower + (args.theta_upper - args.theta_lower) * torch.rand([n_unsolved], dtype=torch.float, device=device)
            process_theta = 2 * math.pi * torch.rand([n_unsolved], dtype=torch.float, device=device)
            rotate_theta = 2 * math.pi * torch.rand([n_unsolved], dtype=torch.float, device=device)

            # solve transformation

            rotation_local = torch.zeros([n_unsolved, 3, 3], dtype=torch.float, device=device)
            rotation_global = torch.zeros([n_unsolved, 3, 3], dtype=torch.float, device=device)
            for j in range(n_unsolved):
                rotation_local[j] = torch.tensor(transforms3d.euler.euler2mat(process_theta[j], deviate_theta[j], rotate_theta[j], axes='rzxz'), dtype=torch.float, device=device)
                rotation_global[j] = torch.tensor(transforms3d.euler.euler2mat(math.atan2(n[j, 1], n[j, 0]) - math.pi / 2, -math.acos(n[j, 2]), 0, axes='rzxz'), dtype=torch.float, device=device)
            translation[unsolved_indices] = p - distance.unsqueeze(1) * (rotation_global @ rotation_local @ torch.tensor([0, 0, 1], dtype=torch.float, device=device).reshape(1, -1, 1)).squeeze(2)
            rotation_hand = torch.tensor(transforms3d.euler.euler2mat(0, -np.pi / 3, 0, axes='rzxz'), dtype=torch.float, device=device)
            rotation[unsolved_indices] = rotation_global @ rotation_local @ rotation_hand

            # determine plane penetration

            # mask_solved[unsolved_indices] = (object_model.plane_parameters[unsolved_indices, :3] * translation[unsolved_indices]).sum(dim=1) + object_model.plane_parameters[unsolved_indices, 3] > 0
            plane_origin = -object_model.plane_parameters[unsolved_indices, :3] * object_model.plane_parameters[unsolved_indices, 3:]
            angle = torch.arccos((torch.nn.functional.normalize(translation[unsolved_indices] - plane_origin) * object_model.plane_parameters[unsolved_indices, :3]).sum(1))
            mask_solved[unsolved_indices] = angle < args.angle_upper
    
    # sample hand qpos

    joint_angles_mu = torch.tensor([0.1, 0, 0.6, 0, 0, 0, 0.6, 0, -0.1, 0, 0.6, 0, 0, -0.2, 0, 0.6, 0, 0, 1.2, 0, -0.2, 0], dtype=torch.float, device=device)
    joint_angles_sigma = args.jitter_strength * (hand_model.joints_upper - hand_model.joints_lower)
    joint_angles = torch.zeros([total_batch_size, hand_model.n_dofs], dtype=torch.float, device=device)
    for i in range(hand_model.n_dofs):
        torch.nn.init.trunc_normal_(joint_angles[:, i], joint_angles_mu[i], joint_angles_sigma[i], hand_model.joints_lower[i] - 1e-6, hand_model.joints_upper[i] + 1e-6)

    hand_pose = torch.cat([
        translation,
        rotation.transpose(1, 2)[:, :2].reshape(-1, 6),
        joint_angles
    ], dim=1)
    hand_pose.requires_grad_()

    contact_point_indices = torch.randint(hand_model.n_contact_candidates, size=[total_batch_size, args.n_contact], device=device)

    hand_model.set_parameters(hand_pose, contact_point_indices)
