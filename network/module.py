import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from misc import ops


class ActNorm(nn.Module):
    def __init__(self, num_channels, scale=1., logscale_factor=3., batch_variance=False):
        """
        Activation normalization layer

        :param num_channels: number of channels
        :type num_channels: int
        :param scale: scale
        :type scale: float
        :param logscale_factor: factor for logscale
        :type logscale_factor: float
        :param batch_variance: use batch variance
        :type batch_variance: bool
        """
        super().__init__()
        self.num_channels = num_channels
        self.scale = scale
        self.logscale_factor = logscale_factor
        self.batch_variance = batch_variance

        self.bias_inited = False
        self.logs_inited = False
        self.register_parameter('bias', nn.Parameter(torch.zeros(1, self.num_channels, 1, 1)))
        self.register_parameter('logs', nn.Parameter(torch.zeros(1, self.num_channels, 1, 1)))

    def actnorm_center(self, x, reverse=False):
        """
        center operation of activation normalization

        :param x: input
        :type x: torch.Tensor
        :param reverse: whether to reverse bias
        :type reverse: bool
        :return: centered input
        :rtype: torch.Tensor
        """
        if not self.bias_inited:
            self.initialize_bias(x)
        if not reverse:
            return x + self.bias
        else:
            return x - self.bias

    def actnorm_scale(self, x, logdet, reverse=False):
        """
        scale operation of activation normalization

        :param x: input
        :type x: torch.Tensor
        :param logdet:
        :type logdet:
        :param reverse: whether to reverse bias
        :type reverse: bool
        :return: centered input and logdet
        :rtype: tuple(torch.Tensor, torch.Tensor)
        """

        if not self.logs_inited:
            self.initialize_logs(x)

        # TODO condition for non 4-dims input
        logs = self.logs * self.logscale_factor

        if not reverse:
            x = x * torch.exp(logs)
        else:
            x = x * torch.exp(-logs)

        if logdet is not None:
            logdet_factor = int(x.shape[2]) * int(x.shape[3])  # H * W
            dlogdet = torch.sum(logs) * logdet_factor
            if reverse:
                dlogdet *= -1
            logdet += dlogdet

        return x, logdet

    def initialize_bias(self, x):
        """
        Initialize bias

        :param x: input
        :type x: torch.Tensor
        """
        if not self.training:
            return
        with torch.no_grad():
            # Compute initial value
            x_mean = -1. * ops.reduce_mean(x, dim=[0, 2, 3], keepdim=True)
            # Copy to parameters
            self.bias.data.copy_(x_mean.data)
            self.bias_inited = True

    def initialize_logs(self, x):
        """
        Initialize logs

        :param x: input
        :type x: torch.Tensor
        """
        if not self.training:
            return
        with torch.no_grad():
            if self.batch_variance:
                x_var = ops.reduce_mean(x ** 2, keepdim=True)
            else:
                x_var = ops.reduce_mean(x ** 2, dim=[0, 2, 3], keepdim=True)
            logs = torch.log(self.scale / (torch.sqrt(x_var) + 1e-6)) / self.logscale_factor

            # Copy to parameters
            self.logs.data.copy_(logs.data)
            self.logs_inited = True

    def forward(self, x, logdet=None, reverse=False):
        """
        Forward activation normalization layer

        :param x: input
        :type x: torch.Tensor
        :param logdet:
        :type logdet:
        :param reverse: whether to reverse bias
        :type reverse: bool
        :return: normalized input and logdet
        :rtype: tuple(torch.Tensor, torch.Tensor)
        """
        assert len(x.shape) == 4
        assert x.shape[1] == self.num_channels, \
            'Input shape should be NxCxHxW, however channels are {} instead of {}'.format(x.shape[1], self.num_channels)

        if not reverse:
            # center and scale
            x = self.actnorm_center(x, reverse)
            x, logdet = self.actnorm_scale(x, logdet, reverse)
        else:
            # scale and center
            x, logdet = self.actnorm_scale(x, logdet, reverse)
            x = self.actnorm_center(x, reverse)
        return x, logdet


class LinearZeros(nn.Linear):
    def __init__(self, in_features, out_features, bias=True, logscale_factor=3.):
        """
        Linear layer with zero initialization

        :param in_features: size of each input sample
        :type in_features: int
        :param out_features: size of each output sample
        :type out_features: int
        :param bias: whether to learn an additive bias.
        :type bias: bool
        :param logscale_factor: factor of logscale
        :type logscale_factor: float
        """
        super().__init__(in_features, out_features, bias)
        self.logscale_factor = logscale_factor
        # zero initialization
        self.weight.data.zero_()
        self.bias.data.zero_()
        # register parameter
        self.register_parameter('logs', nn.Parameter(torch.zeros(out_features)))

    def forward(self, x):
        """
        Forward linear zero layer

        :param x: input
        :type x: torch.Tensor
        :return: output
        :rtype: torch.Tensor
        """
        output = super().forward(x)
        output *= torch.exp(self.logs * self.logscale_factor)
        return output


class Conv2d(nn.Conv2d):
    @staticmethod
    def get_padding(padding_type, kernel_size, stride):
        """
        Get padding size.

        mentioned in https://github.com/pytorch/pytorch/issues/3867#issuecomment-361775080
        behaves as 'SAME' padding in TensorFlow
        independent on input size when stride is 1

        :param padding_type: type of padding in ['SAME', 'VALID']
        :type padding_type: str
        :param kernel_size: kernel size
        :type kernel_size: tuple(int) or int
        :param stride: stride
        :type stride: int
        :return: padding size
        :rtype: tuple(int)
        """
        assert padding_type in ['SAME', 'VALID'], "Unsupported padding type: {}".format(padding_type)
        if isinstance(kernel_size, int):
            kernel_size = [kernel_size, kernel_size]
        if padding_type == 'SAME':
            assert stride == 1, "'SAME' padding only supports stride=1"
            return tuple((k - 1) // 2 for k in kernel_size)
        return tuple(0 for _ in kernel_size)

    def __init__(self, in_channels, out_channels,
                 kernel_size=(3, 3), stride=1, padding_type='SAME',
                 do_weightnorm=False, do_actnorm=True,
                 dilation=1, groups=1):
        """
        Wrapper of nn.Conv2d with weight normalization and activation normalization

        :param padding_type: type of padding in ['SAME', 'VALID']
        :type padding_type: str
        :param do_weightnorm: whether to do weight normalization after convolution
        :type do_weightnorm: bool
        :param do_actnorm: whether to do activation normalization after convolution
        :type do_actnorm: bool
        """
        padding = self.get_padding(padding_type, kernel_size, stride)
        super().__init__(in_channels, out_channels,
                         kernel_size, stride, padding,
                         dilation, groups,
                         bias=(not do_actnorm))
        self.do_weight_norm = do_weightnorm
        self.do_actnorm = do_actnorm

        self.weight.data.normal_(mean=0.0, std=0.05)
        if self.do_actnorm:
            self.actnorm = ActNorm(out_channels)
        else:
            self.bias.data.zero_()

    def forward(self, x):
        """
        Forward wrapped Conv2d layer

        :param x: input
        :type x: torch.Tensor
        :return: output
        :rtype: torch.Tensor
        """
        x = super().forward(x)
        if self.do_weight_norm:
            # normalize N, H and W dims
            F.normalize(x, p=2, dim=0)
            F.normalize(x, p=2, dim=2)
            F.normalize(x, p=2, dim=3)
        if self.do_actnorm:
            x, _ = self.actnorm(x)
        return x


class Conv2dZeros(nn.Conv2d):

    def __init__(self, in_channels, out_channels,
                 kernel_size=(3, 3), stride=1, padding_type='SAME',
                 logscale_factor=3,
                 dilation=1, groups=1, bias=True):
        """
        Wrapper of nn.Conv2d with zero initialization and logs

        :param padding_type: type of padding in ['SAME', 'VALID']
        :type padding_type: str
        :param logscale_factor: factor for logscale
        :type logscale_factor: float
        """
        padding = Conv2d.get_padding(padding_type, kernel_size, stride)
        super().__init__(in_channels, out_channels, kernel_size, stride, padding, dilation, groups, bias)

        self.logscale_factor = logscale_factor
        # initialize variables with zero
        self.bias.data.zero_()
        self.weight.data.zero_()
        self.register_parameter("logs", nn.Parameter(torch.zeros(out_channels, 1, 1)))

    def forward(self, x):
        """
        Forward wrapped Conv2d layer

        :param x: input
        :type x: torch.Tensor
        :return: output
        :rtype: torch.Tensor
        """
        x = super().forward(x)
        x *= torch.exp(self.logs * self.logscale_factor)
        return x


def f(in_channels, hidden_channels, out_channels):
    """
    Convolution block

    :param in_channels: number of input channels
    :type in_channels: int
    :param hidden_channels: number of hidden channels
    :type hidden_channels: int
    :param out_channels: number of output channels
    :type out_channels: int
    :return: desired convolution block
    :rtype: nn.Module
    """
    return nn.Sequential(
        Conv2d(in_channels, hidden_channels),
        nn.ReLU(inplace=True),
        Conv2d(hidden_channels, hidden_channels, kernel_size=1),
        nn.ReLU(inplace=True),
        Conv2dZeros(hidden_channels, out_channels)
    )


class Invertible1x1Conv(nn.Module):

    def __init__(self, num_channels, lu_decomposed=False):
        """
        Invertible 1x1 convolution layer

        :param num_channels: number of channels
        :type num_channels: int
        :param lu_decomposed: whether to use LU decomposition
        :type lu_decomposed: bool
        """
        super().__init__()
        self.num_channels = num_channels
        self.lu_decomposed = lu_decomposed
        if self.lu_decomposed:
            raise NotImplementedError()
        else:
            w_shape = [num_channels, num_channels]
            # Sample a random orthogonal matrix
            w_init = np.linalg.qr(np.random.randn(*w_shape))[0].astype('float32')
            self.register_parameter('weight', nn.Parameter(torch.Tensor(w_init)))

    def forward(self, x, logdet=None, reverse=False):
        """

        :param x: input
        :type x: torch.Tensor
        :param logdet:
        :type logdet:
        :param reverse: whether to reverse bias
        :type reverse: bool
        :return: output and logdet
        :rtype: tuple(torch.Tensor, torch.Tensor)
        """
        logdet_factor = x.shape[1] * x.shape[2]  # H * W
        dlogdet = torch.log(torch.abs(torch.det(self.weight))) * logdet_factor
        if not reverse:
            weight = self.weight.view(*self.weight.shape, 1, 1)
            z = F.conv2d(x, weight)
            if logdet is not None:
                logdet += dlogdet
            return z, logdet
        else:
            weight = self.weight.inverse().view(*self.weight.shape, 1, 1)
            z = F.conv2d(x, weight)
            if logdet is not None:
                logdet -= dlogdet
            return z, logdet


class Permutation2d(nn.Module):

    def __init__(self, num_channels, shuffle=False):
        """
        Perform permutation on channel dimension

        :param num_channels:
        :type num_channels:
        :param shuffle:
        :type shuffle:
        """
        super().__init__()
        self.num_channels = num_channels
        self.indices = np.arange(self.num_channels - 1, -1, -1, dtype=np.long)
        if shuffle:
            np.random.shuffle(self.indices)
        self.indices_inverse = np.zeros(self.num_channels, dtype=np.long)
        for i in range(self.num_channels):
            self.indices_inverse[self.indices[i]] = i

    def forward(self, x, reverse=False):
        assert len(x.shape) == 4
        if not reverse:
            return x[:, self.indices, :, :]
        else:
            return x[:, self.indices_inverse, :, :]


class GaussianDiag:
    """
    Generator of gaussian diagonal matrix
    """

    @staticmethod
    def eps(mean):
        """
        Returns a tensor filled with random numbers from a standard normal distribution

        :param mean: input tensor
        :type mean: torch.Tensor
        :return: a tensor filled with random numbers from a standard normal distribution
        :rtype: torch.Tensor
        """
        return torch.randn_like(mean)

    @staticmethod
    def flatten_sum(tensor):
        """
        Summarize tensor except first dimension

        :param tensor: input tensor
        :type tensor: torch.Tensor
        :return: summarized tensor
        :rtype: torch.Tensor
        """
        assert len(tensor.shape) == 4
        return ops.reduce_sum(tensor, dim=[1, 2, 3])

    @staticmethod
    def logps(mean, logs, x):
        """
        Likehood

        :param mean:
        :type mean: torch.Tensor
        :param logs:
        :type logs: torch.Tensor
        :param x: input tensor
        :type x: torch.Tensor
        :return: likehood
        :rtype: torch.Tensor
        """
        return -0.5 * (np.log(2 * np.pi) + 2. * logs + (x - mean) ** 2 / torch.exp(2. * logs))

    @staticmethod
    def logp(mean, logs, x):
        """
        Summarized likehood

        :param mean:
        :type mean: torch.Tensor
        :param logs:
        :type logs: torch.Tensor
        :param x: input tensor
        :type x: torch.Tensor
        :return:
        :rtype: torch.Tensor
        """
        s = GaussianDiag.logps(mean, logs, x)
        return GaussianDiag.flatten_sum(s)

    @staticmethod
    def sample(mean, logs):
        """
        Generate smaple

        :type mean: torch.Tensor
        :param logs:
        :type logs: torch.Tensor
        :return: sample
        :rtype: torch.Tensor
        """
        eps = GaussianDiag.eps(mean)
        return mean + torch.exp(logs) * eps


class Split2d(nn.Module):
    def __init__(self, num_channels):
        """
        Split2d layer

        :param num_channels: number of channels
        :type num_channels: int
        """
        super().__init__()
        self.num_channels = num_channels
        self.conv2d_zeros = Conv2dZeros(num_channels // 2, num_channels)

    def prior(self, z):
        """
        Pre-process

        :param z: input tensor
        :type z: torch.Tensor
        :return: output tensor
        :rtype: torch.Tensor
        """
        h = self.conv2d_zeros(z)
        mean = h[:, 0::2, :, :]
        logs = h[:, 1::2, :, :]
        return mean, logs

    def forward(self, x, logdet=None, reverse=False):
        """
        Forward Split2d layer

        :param x: input tensor
        :type x: torch.Tensor
        :param logdet:
        :type logdet:
        :param reverse: whether to reverse flow
        :type reverse: bool
        :return: output and logdet
        :rtype: tuple(torch.Tensor, torch.Tensor)
        """
        if not reverse:
            nc = input.shape[1]
            z1 = input[:, :nc // 2, :, :]
            z2 = input[:, nc // 2:, :, :]
            mean, logs = self.prior(z1)
            logdet += GaussianDiag.logp(mean, logs, z2)
            return z1, logdet
        else:
            z1 = x
            mean, logs = self.prior(z1)
            z2 = GaussianDiag.sample(mean, logs)
            z = torch.cat((z1, z2), dim=1)
            return z, logdet


class Squeeze2d(nn.Module):
    def __init__(self, factor=2):
        """
        Squeeze2d layer

        :param factor: squeeze factor
        :type factor: int
        """
        super().__init__()
        self.factor = factor

    @staticmethod
    def unsqueeze(x, factor=2):
        """
        Unsqueeze tensor

        :param x: input tensor
        :type x: torch.Tensor
        :param factor: unsqueeze factor
        :type factor: int
        :return: unsqueezed tensor
        :rtype: torch.Tensor
        """
        assert factor >= 1
        if factor == 1:
            return x
        nc = x.shape[1]
        nh = x.shape[2]
        nw = x.shape[3]
        assert nc >= 4 and nc % 4 == 0
        x = x.view(-1, int(nc / factor ** 2), factor, factor, nh, nw)
        x = x.permute(0, 1, 4, 2, 5, 3).contiguous()
        x = x.view(-1, int(nc / factor ** 2), int(nh * factor), int(nw * factor))
        return x

    @staticmethod
    def squeeze(x, factor=2):
        """
        Squeeze tensor

        :param x: input tensor
        :type x: torch.Tensor
        :param factor: squeeze factor
        :type factor: int
        :return: squeezed tensor
        :rtype: torch.Tensor
        """
        assert factor >= 1
        if factor == 1:
            return x
        nc = x.shape[1]
        nh = x.shape[2]
        nw = x.shape[3]
        assert nh % factor == 0 and nw % factor == 0
        x = x.view(-1, nc, nh // factor, factor, nw // factor, factor)
        x = x.permute([0, 1, 3, 5, 2, 4]).contiguous()
        x = x.view(-1, nc * factor * factor, nh // factor, nw // factor)
        return x

    def forward(self, x, logdet=None, reverse=False):
        """
        Forward Squeeze2d layer

        :param x: input tensor
        :type x: torch.Tensor
        :param logdet:
        :type logdet:
        :param reverse: whether to reverse flow
        :type reverse: bool
        :return: output and logdet
        :rtype: tuple(torch.Tensor, torch.Tensor)
        """
        if not reverse:
            output = self.squeeze(x, self.factor)
        else:
            output = self.unsqueeze(x, self.factor)

        return output, logdet
