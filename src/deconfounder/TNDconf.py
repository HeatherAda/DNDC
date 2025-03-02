import torch
import torch.nn as nn
import torch.nn.functional as F
from pygcn.layers import GraphConvolution
from torch.autograd import Variable, Function

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

class ReverseLayerF(Function):
    @staticmethod
    def forward(ctx, x, alpha):
        ctx.alpha = alpha
        return x.view_as(x)

    @staticmethod
    def backward(ctx, grad_output):
        output = grad_output.neg() * ctx.alpha
        return output, None

# The time-series network-based deconfounder
class TNDconf(nn.Module):
    def __init__(self, x_dim, h_dim, z_dim, n_layers_gcn, n_out, dropout, alpha, P):
        super(TNDconf, self).__init__()

        self.x_dim = x_dim  # feature dim
        #self.eps = eps  # ?
        self.h_dim = h_dim
        self.z_dim = z_dim
        self.n_layers_gcn = n_layers_gcn
        self.n_out = n_out
        self.dropout = dropout
        self.alpha = alpha
        self.P = P

        self.phi_x = nn.Sequential(nn.Linear(x_dim, h_dim).to(device), nn.ReLU().to(device))  # nn.BatchNorm1d(h_dim)
        self.gc = [GraphConvolution(h_dim, h_dim).to(device)]
        for i in range(n_layers_gcn - 1):
            self.gc.append(GraphConvolution(h_dim, h_dim).to(device))
        self.fuse = nn.Sequential(nn.Linear(h_dim + h_dim, z_dim).to(device), nn.ReLU().to(device))  # phi1, z2, z3 => z

        # prediction
        # potential outcome
        self.out_t00 = [nn.Linear(z_dim, z_dim).to(device) for i in range(n_out)]
        self.out_t10 = [nn.Linear(z_dim, z_dim).to(device) for i in range(n_out)]
        self.out_t01 = nn.Linear(z_dim, 1).to(device)
        self.out_t11 = nn.Linear(z_dim, 1).to(device)

        # propensity score
        self.ps_predictor = nn.Sequential()
        self.ps_predictor.add_module('d_fc1', nn.Linear(z_dim, 100).to(device))
        self.ps_predictor.add_module('d_bn1', nn.BatchNorm1d(100).to(device))
        #self.ps_predictor.add_module('d_relu1', nn.ReLU(True).to(device))
        self.ps_predictor.add_module('d_sigmoid1', nn.Sigmoid().to(device))
        self.ps_predictor.add_module('d_fc2', nn.Linear(100, 2).to(device))
        self.ps_predictor.add_module('d_softmax', nn.Softmax(dim=1).to(device))

        # memory unit
        self.rnn = nn.GRUCell(z_dim+1, h_dim).to(device)  # c_t, z_t, h_{t-1} => h_t
        #self.rnn = nn.LSTMCell(z_dim + 1, h_dim).to(device)  # c_t, z_t, h_{t-1} => h_t

    def forward(self, X_list, A_list, C_list, hidden_in=None):
        '''
        :param X_list:  list of torch.FloatTensor
        :param A_list:  list of torch.sparse.FloatTensor
        :param C_list:  list of torch.FloatTensor
        :param hidden_in:
        :return:
        '''
        all_z_t = []
        all_y1 = []  # predicted potential outcome
        all_y0 = []
        all_ps = []  # predicted propensity score

        num_timestep = len(X_list)
        num_node = X_list[-1].size(0)  # node number: the size of nodes in the last time step

        if hidden_in is None:
            h = Variable(torch.zeros(num_node, self.h_dim))  # h: n_T x feat
        else:
            h = Variable(hidden_in)

        h = h.to(device)

        for t in range(num_timestep):  # time step
            C = C_list[t]
            x = X_list[t]

            adj = A_list[t]

            phi_x_t = self.phi_x(x)

            # gcn
            rep = F.relu(self.gc[0](phi_x_t, adj))
            rep = F.dropout(rep, self.dropout, training=self.training)
            for i in range(1, self.n_layers_gcn):
                rep = F.relu(self.gc[i](rep, adj))
                rep = F.dropout(rep, self.dropout, training=self.training)

            # fuse phi_1, z_2, z_3
            z_t = self.fuse(torch.cat([h, rep], 1))
            #z_t = h

            # C
            C_float = C.type(torch.FloatTensor).view(-1, 1)
            C_float = C_float.to(device)

            # RNN
            h = self.rnn(torch.cat([z_t, C_float], 1), h)

            # prediction
            # potential outcome
            for i in range(self.n_out):
                y00 = F.relu(self.out_t00[i](z_t))
                y00 = F.dropout(y00, self.dropout, training=self.training)
                y10 = F.relu(self.out_t10[i](z_t))
                y10 = F.dropout(y10, self.dropout, training=self.training)

            y0 = self.out_t01(y00).view(-1)
            y1 = self.out_t11(y10).view(-1)

            #y = torch.where(C > 0, y1, y0)

            # treatment prediction
            reverse_feature = ReverseLayerF.apply(z_t, self.alpha)
            ps_hat = self.ps_predictor(reverse_feature)  # [:,1]

            # record
            all_z_t.append(z_t)
            #all_y.append(y)
            all_y1.append(y1)
            all_y0.append(y0)
            all_ps.append(ps_hat)

        return all_y1, all_y0, all_z_t, all_ps, h




