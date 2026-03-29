需要通过ADB（Android Debug Bridge）连接云手机时，请按照本文所述步骤进行配置。

## 准备工作

1.  在无影云手机控制台上创建或导入密钥对，并将密钥对绑定至对应的云手机实例。具体操作，请参见[管理密钥](https://help.aliyun.com/zh/ecp/manage-keys)。
    
2.  将该密钥对的私钥adbkey存储在以下目录，确保能在本地通过ADB登录。
    
    -   macOS操作系统下的目录：`~/.android`
        
    -   Windows操作系统下的目录：`%USERPROFILE%\.android`
        
3.  执行以下命令来重启本地的ADB进程。
    
    ```
    adb kill-server
    ```
    
    ```
    adb start-server
    ```
    

## **操作步骤**

**说明**

-   若云手机实例所属的实例组的网络类型为**共享网络**，则仅支持通过私网ADB连接。
    
-   若云手机实例所属的实例组的网络类型为**VPC网络**，则支持一键ADB（推荐），也支持通过私网ADB或公网ADB连接。
    

#### **一键ADB（推荐）**

##### 前提条件

实例所属的VPC网络已开通互联网访问。具体操作，请参见[管理VPC网络的互联网访问权限](https://help.aliyun.com/zh/ecp/untitled-document-1718174642933#sc-vpc-internet-access)。

##### **操作步骤**

1.  登录[无影云手机控制台](https://wya.wuying.aliyun.com/)。
    
2.  在左侧导航栏，选择**资源管理** > **实例**。
    
3.  在**实例**页面上，找到目标实例，在**操作**列中单击 ⋮ 图标，并选择**一键ADB**。
    
4.  在**一键ADB连接**面板上，单击**一键创建ADB连接**。
    
    **说明**
    
    若该VPC网络尚未开通互联网访问，则请在提示弹窗中单击**立即前往开通**。
    
    ![panel_one_click_adb](https://help-static-aliyun-doc.aliyuncs.com/assets/img/zh-CN/5429541571/p968834.png)
    
5.  稍等片刻后，ADB连接将自动创建成功。您可以单击**ADB连接方式**右侧的图标来复制ADB连接命令。
    
    ![panel_one_click_adb_created](https://help-static-aliyun-doc.aliyuncs.com/assets/img/zh-CN/5429541571/p968876.png)
    

##### 后续步骤

一键ADB功能仅提供一键ADB连接方式，但您仍需正确配置安全组才能连接到实例。请确保您的安全组已开放源地址至云手机入方向5555端口的TCP连接。具体操作，请参见[公网ADB连接](#div-adb-internet)。

#### **私网ADB连接**

1.  连接办公网络所在VPC内的无影云电脑。
    
2.  执行以下命令连接云手机。
    
    ```
    adb connect <192.168.XX.XX>:5555
    ```
    
    **说明**
    
    请将`<192.168.XX.XX>`替换为云手机的内网IP地址。
    
    **如何查询云手机的内网IP地址？**
    
    1.  登录[无影云手机控制台](https://wya.wuying.aliyun.com/)。
        
    2.  在左侧导航栏，选择**资源管理** > **实例**。
        
    3.  在**实例**页面的列表中找到该云手机实例，并复制**内网IP**列的值。
        
        ![f_private_ip_address.png](https://help-static-aliyun-doc.aliyuncs.com/assets/img/zh-CN/4018031371/p870822.png)
        
    

#### **公网ADB连接**

如果需要本地设备通过公网adb访问云手机，则需要增加一个DNAT，并修改安全组配置。

1.  在云手机实例所属的VPC下创建公网NAT网关。具体操作，请参见[云手机如何访问互联网](https://help.aliyun.com/zh/ecp/how-cloud-phones-access-the-internet)。如已创建，请跳过此步骤。
    
2.  为上述公网NAT网关创建DNAT条目，并配置端口映射规则。
    
    1.  登录[NAT网关管理控制台](https://vpc.console.aliyun.com/nat)。
        
    2.  在**公网NAT网关**页面上找到该公网NAT网关实例，并在**操作**列单击**设置DNAT**。
        
    3.  在**DNAT管理**页签上单击**创建DNAT条目**。
        
    4.  在**创建DNAT条目**页面上完成以下配置：
        
        ![pg_create_dnat_entry.png](https://help-static-aliyun-doc.aliyuncs.com/assets/img/zh-CN/9442370371/p867197.png)
        
        -   **选择弹性公网IP地址**：选择一个可用的地址，并记录该弹性公网IP，通过命令连接云手机时将用到此IP。
            
        -   **选择私网IP地址**：选择**通过手动输入**，并填写云手机实例的**内网IP**。
            
            **如何查询云手机的内网IP地址？**
            
            1.  登录[无影云手机控制台](https://wya.wuying.aliyun.com/)。
                
            2.  在左侧导航栏，选择**资源管理** > **实例**。
                
            3.  在**实例**页面的列表中找到该云手机实例，并复制**内网IP**列的值。
                
                ![f_private_ip_address.png](https://help-static-aliyun-doc.aliyuncs.com/assets/img/zh-CN/4018031371/p870822.png)
                
            
        -   **具体端口**：填写要映射的公网和私网端口，例如`1000:5555`。
            
3.  修改弹性网卡的`policy`安全组的规则，将5555端口开放给公网访问。
    
    1.  登录[ECS控制台](https://ecs.console.aliyun.com)。
        
    2.  在左侧导航栏中选择**网络与安全** > **弹性网卡**。
        
    3.  在**弹性网卡**页面单击弹性网卡ID，并单击**基本信息**区域内的第一个安全组ID。
        
        **说明**
        
        在弹性网卡绑定的2个安全组当中，一个名称为`vda`，另一个名称为`policy`，需要修改规则的是名称为`policy`的安全组。单击安全组ID之后即可查看该安全组的名称。
        
        ![pg_eni_default_security_group_policy.png](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=)
        
    4.  在**安全组详情**页签的**入方向**页签上单击**手动添加**，并配置以下规则：
        
        ![pg_security_group_for_dnat.png](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=)
        
        -   **授权策略**：允许
            
        -   **优先级**：1
            
        -   **协议类型**：自定义TCP
            
        -   **端口范围**：`5555/5555`
            
        -   **授权对象**：`0.0.0.0/0`
            
            **说明**
            
            此配置表示所有IP地址都可以访问5555端口。若希望提高安全性，也可以填写将执行adb命令的本地设备的公网IP地址。
            
4.  执行以下命令连接云手机。
    
    ```
    adb connect <公网IP>:<DNAT公网端口>
    ```
    
    **说明**
    
    请将`<公网IP>`替换为DNAT绑定的弹性公网IP，将`<DNAT公网端口>`替换为DNAT的公网端口，在本文示例中为`1000`。
    

## **常见问题**

### **通过公网NAT连接ADB，遇到网络不通或超时问题，怎么办？**

1.  请确认执行的命令是否正确。通过公网NAT连接ADB的命令为：
    
    ```
    adb connect <公网IP>:<DNAT公网端口>
    ```
    
2.  若命令无误，请从以下方面排查：
    
    -   确认是否已创建对应的公网DNAT条目。
        
    -   确认公网IP和端口是否正确。
        
    -   确认安全组是否已放行对应的端口。
        
    -   确认VPC内的路由表配置是否正确，主要关注NAT路由表的下一跳是否已配置为SNAT的公网NAT网关。
        
3.  如果仍然无法解决问题，您可以提交[工单](https://smartservice.console.aliyun.com/service/create-ticket?product=gws)以获取阿里云技术支持。
    

### **通过**ADB**连接云手机时发生鉴权失败错误，怎么办？**

1.  请确认是否已绑定密钥对，并且已下载对应的adbkey文件到本地的对应目录。
    
2.  绑定密钥对并将私钥下载到对应目录后，必须执行以下命令来重启本地的ADB服务。
    
    ```
    adb kill-server
    ```
    
    ```
    adb start-server
    ```